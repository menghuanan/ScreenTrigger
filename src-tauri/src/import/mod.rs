//! 图片导入：文件选择器 token 的一次性签发与绑定（TASK-009，M01 `import_image`）。
//!
//! 安全模型（01/03）：**token 只能由文件选择器生成**——UI 触发 [`pick_image_file`]
//! 打开系统文件对话框，Rust 侧把选中路径绑定为一次性 token（HMAC-SHA256 签名，
//! 会话绑定 + TTL），返回给前端；前端经既有 `ipc_request` 发
//! `import_image {import_token}` 给 Python 侧消费。**脚本永远拿不到文件路径**。
//!
//! 密钥共享：应用启动时为每次运行生成随机密钥（[`ImportTokenIssuer::ephemeral`]），
//! 经 [`install_shared_secret_hex`] 存入进程全局，sidecar spawn 时以
//! `ST_IMPORT_TOKEN_SECRET` 注入，Python `HmacTokenAuthority` 据此校验签名。
//!
//! token 线格式与 Python 侧逐字一致，见 [`token`] 模块（含跨语言固定向量测试）。

pub mod token;

use std::sync::OnceLock;

use serde::Serialize;
use tauri::State;

use crate::sidecar::SupervisorHandle;

/// 默认 TTL（秒）：选择器 → 导入的一次性窗口。
pub const DEFAULT_TTL_SECONDS: i64 = 300;

/// 导入命令错误（06 四字段子集：code/message/retryable）。
#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct ImportError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
}

// ---------------------------------------------------------------------------
// 进程级共享密钥（供 sidecar spawn 注入 ST_IMPORT_TOKEN_SECRET）
// ---------------------------------------------------------------------------

static SHARED_SECRET_HEX: OnceLock<String> = OnceLock::new();

/// 安装共享密钥（十六进制）；重复安装返回 false（幂等，首次生效）。
pub fn install_shared_secret_hex(secret_hex: String) -> bool {
    SHARED_SECRET_HEX.set(secret_hex).is_ok()
}

/// 读取共享密钥（未安装返回 None → sidecar spawn 不注入该环境变量）。
pub fn shared_secret_hex() -> Option<&'static str> {
    SHARED_SECRET_HEX.get().map(|s| s.as_str())
}

// ---------------------------------------------------------------------------
// 签发者
// ---------------------------------------------------------------------------

/// 导入 token 签发者：持有 HMAC 密钥与 TTL，按当前引擎会话签发一次性 token。
pub struct ImportTokenIssuer {
    secret: Vec<u8>,
    ttl_seconds: i64,
}

impl ImportTokenIssuer {
    /// 以给定密钥构造。
    pub fn new(secret: Vec<u8>, ttl_seconds: i64) -> Self {
        Self { secret, ttl_seconds }
    }

    /// 每次运行生成随机密钥（两个 uuid-v4 = 32 字节熵）。密钥随进程结束失效，
    /// 故 token 天然无法跨应用运行重放。
    pub fn ephemeral() -> Self {
        let mut secret = Vec::with_capacity(32);
        secret.extend_from_slice(uuid::Uuid::new_v4().as_bytes());
        secret.extend_from_slice(uuid::Uuid::new_v4().as_bytes());
        Self::new(secret, DEFAULT_TTL_SECONDS)
    }

    /// 密钥的十六进制表示（注入 sidecar 环境用）。
    ///
    /// **注意**：hex 仅是**传输编码**——签名 canonical key 始终为 [`Self::secret`]
    /// 的**原始字节**（Rust `issue()` 用 `&self.secret` 直接作 HMAC key）。消费端
    /// （Python `HmacTokenAuthority`）须先把注入的 hex **解码回 raw 字节**再作 key，
    /// 否则两侧密钥表示不一致、签发的 token 会被全部拒绝（DEF-009-1）。
    pub fn secret_hex(&self) -> String {
        self.secret.iter().map(|b| format!("{b:02x}")).collect()
    }

    /// 为（会话, 路径）签发一次性 token。
    pub fn issue(&self, session_id: &str, path: &str) -> String {
        let nonce = uuid::Uuid::new_v4().simple().to_string();
        token::mint_import_token(
            &self.secret,
            session_id,
            path,
            token::now_epoch(),
            self.ttl_seconds,
            &nonce,
        )
    }

    /// 指定时刻/随机数签发（测试用）。
    pub fn issue_with(&self, session_id: &str, path: &str, now: i64, nonce: &str) -> String {
        token::mint_import_token(&self.secret, session_id, path, now, self.ttl_seconds, nonce)
    }
}

// ---------------------------------------------------------------------------
// Tauri 命令
// ---------------------------------------------------------------------------

/// 打开系统文件对话框选择图片，返回一次性导入 token（取消返回 `null`）。
///
/// 需引擎已启动（token 绑定当前 session_id）；未启动返回 INVALID_STATE。
#[tauri::command]
pub fn pick_image_file(
    supervisor: State<'_, SupervisorHandle>,
    issuer: State<'_, ImportTokenIssuer>,
) -> Result<Option<String>, ImportError> {
    let status = supervisor.status();
    let session_id = status.session_id.ok_or_else(|| ImportError {
        code: "INVALID_STATE".to_string(),
        message: "引擎未启动：无法签发导入 token".to_string(),
        retryable: false,
    })?;
    match pick_native_image_file() {
        None => Ok(None),
        Some(path) => Ok(Some(issuer.issue(&session_id, &path))),
    }
}

/// 打开系统文件选择对话框（Windows 通用对话框 GetOpenFileNameW）。
#[cfg(target_os = "windows")]
fn pick_native_image_file() -> Option<String> {
    use windows::core::{PCWSTR, PWSTR};
    use windows::Win32::UI::Controls::Dialogs::{
        GetOpenFileNameW, OFN_EXPLORER, OFN_FILEMUSTEXIST, OFN_NOCHANGEDIR, OFN_PATHMUSTEXIST,
        OPENFILENAMEW,
    };

    let mut buffer = [0u16; 1024];
    // 过滤器：成对以 NUL 分隔，双 NUL 结束。
    let filter: Vec<u16> = "图片 (*.png;*.jpg;*.jpeg)\0*.png;*.jpg;*.jpeg\0所有文件 (*.*)\0*.*\0\0"
        .encode_utf16()
        .collect();

    let mut ofn = OPENFILENAMEW::default();
    ofn.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
    ofn.lpstrFilter = PCWSTR(filter.as_ptr());
    ofn.lpstrFile = PWSTR(buffer.as_mut_ptr());
    ofn.nMaxFile = buffer.len() as u32;
    ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;

    let confirmed = unsafe { GetOpenFileNameW(&mut ofn) };
    if !confirmed.as_bool() {
        return None; // 用户取消
    }
    let end = buffer.iter().position(|&c| c == 0).unwrap_or(buffer.len());
    if end == 0 {
        return None;
    }
    Some(String::from_utf16_lossy(&buffer[..end]))
}

#[cfg(not(target_os = "windows"))]
fn pick_native_image_file() -> Option<String> {
    None // 非 Windows 平台无文件选择器（本产品目标平台为 Windows）
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn issuer_signs_bound_tokens() {
        let issuer = ImportTokenIssuer::new(b"seed".to_vec(), 300);
        let a = issuer.issue_with("s1", "C:/x/a.png", 1000, "n1");
        let b = issuer.issue_with("s1", "C:/x/a.png", 1000, "n2");
        assert!(a.starts_with(token::TOKEN_PREFIX));
        assert_ne!(a, b, "不同 nonce 的 token 不同");
        assert_eq!(issuer.secret_hex().len(), 8); // "seed" 4 字节
    }
}
