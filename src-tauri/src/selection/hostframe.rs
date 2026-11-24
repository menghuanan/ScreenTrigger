//! 宿主帧 token 的签发与受控临时区读写（DEF-TASK008-01，方向 A 宿主端）。
//!
//! **背景**：`confirm_selection` 的最终帧必须在**引擎**内登记（`SessionImageStore`）
//! 才能被公开命令 `get_image_preview`/`release_image` 解析（03 契约）。因 Router
//! 单线程自锁约束（见 [`crate::ipc::hostcall`]），宿主采用：
//!
//! 1. 把最终帧 PNG 写入数据根下的受控临时区 `<data_dir>/tmp/selection/`；
//! 2. 签发**一次性 host-frame token**（HMAC-SHA256 签名，绑定 文件名/sha256/宽高/
//!    nonce/TTL/会话）——与 TASK-009 import token **同一安全模型**（引擎**只认
//!    token，不接受任意路径**）；
//! 3. 经**内部命令** `register_host_frame {token}` 交引擎；引擎校验后读入、存入
//!    `SessionImageStore`、返回**引擎签发**的 `ImageRef`，并删除临时文件。
//!
//! token 线格式与 Python 侧 `backend/image/host_frame.py` **逐字一致**
//! （见本模块 `token_matches_python_vector` 跨语言固定向量测试）。
//! 零新增依赖：HMAC-SHA256 基于已有 `sha2`（复用 [`crate::import::token`] 的
//! `b64url`/`hmac_sha256`）。

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use serde::Serialize;

use crate::import::token::{b64url, hmac_sha256, now_epoch};

/// token 前缀（与 Python 侧 `HOST_FRAME_TOKEN_PREFIX` 逐字一致）。
pub const HOST_FRAME_TOKEN_PREFIX: &str = "sthost1";

/// 一次性窗口（秒）：宿主签发后立即消费（远短于 import token 的 300s）。
pub const DEFAULT_HOST_FRAME_TTL_SECONDS: i64 = 60;

/// 受控临时区（数据根相对）。
pub const TEMP_SUBDIR: [&str; 2] = ["tmp", "selection"];

/// 载荷字段顺序固定（serde 按结构体字段顺序序列化，与 Python dict 顺序一致：
/// `v, sid, f, sha, w, h, n, exp`）。
#[derive(Serialize)]
struct HostFramePayload<'a> {
    v: u32,
    sid: &'a str,
    f: &'a str,
    sha: &'a str,
    w: i64,
    h: i64,
    n: &'a str,
    exp: i64,
}

/// 签发一次性 host-frame token（**仅供宿主**；脚本不得调用）。
///
/// 与 Python `mint_host_frame_token` 逐字一致：紧凑 JSON（无空格）+ base64url（无填充）。
#[allow(clippy::too_many_arguments)]
pub fn mint_host_frame_token(
    secret: &[u8],
    session_id: &str,
    filename: &str,
    sha256: &str,
    width: i64,
    height: i64,
    now: i64,
    ttl_seconds: i64,
    nonce: &str,
) -> String {
    let payload = HostFramePayload {
        v: 1,
        sid: session_id,
        f: filename,
        sha: sha256,
        w: width,
        h: height,
        n: nonce,
        exp: now + ttl_seconds,
    };
    let json = serde_json::to_string(&payload).expect("HostFramePayload 序列化不应失败");
    let payload_b64 = b64url(json.as_bytes());
    let signature = b64url(&hmac_sha256(secret, payload_b64.as_bytes()));
    format!("{HOST_FRAME_TOKEN_PREFIX}.{payload_b64}.{signature}")
}

/// 受控临时区绝对路径：`<data_dir>/tmp/selection`。
pub fn temp_dir_for(data_dir: &Path) -> PathBuf {
    let mut dir = data_dir.to_path_buf();
    for part in TEMP_SUBDIR {
        dir.push(part);
    }
    dir
}

/// 把最终帧 PNG 写入受控临时区（目录不存在则创建）；返回绝对路径。
pub fn write_temp_frame(data_dir: &Path, filename: &str, png: &[u8]) -> std::io::Result<PathBuf> {
    let dir = temp_dir_for(data_dir);
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(filename);
    std::fs::write(&path, png)?;
    Ok(path)
}

/// 删除受控临时区文件（best-effort；登记失败时宿主回收）。
pub fn remove_temp_frame(data_dir: &Path, filename: &str) {
    let _ = std::fs::remove_file(temp_dir_for(data_dir).join(filename));
}

/// 当前 epoch 秒（复用 import token 时钟）。
pub fn now_epoch_seconds() -> i64 {
    now_epoch()
}

// ---------------------------------------------------------------------------
// 签发者（持有密钥与 TTL）
// ---------------------------------------------------------------------------

/// host-frame token 签发者。
pub struct HostFrameIssuer {
    secret: Vec<u8>,
    ttl_seconds: i64,
}

impl HostFrameIssuer {
    /// 以给定密钥/TTL 构造。
    pub fn new(secret: Vec<u8>, ttl_seconds: i64) -> Self {
        Self { secret, ttl_seconds }
    }

    /// 每次运行生成随机密钥（两个 uuid-v4 = 32 字节熵）。密钥随进程结束失效，
    /// token 天然无法跨应用运行重放。
    pub fn ephemeral() -> Self {
        let mut secret = Vec::with_capacity(32);
        secret.extend_from_slice(uuid::Uuid::new_v4().as_bytes());
        secret.extend_from_slice(uuid::Uuid::new_v4().as_bytes());
        Self::new(secret, DEFAULT_HOST_FRAME_TTL_SECONDS)
    }

    /// 原始密钥字节（签名 canonical key）。
    pub fn secret(&self) -> &[u8] {
        &self.secret
    }

    /// 密钥十六进制表示（注入 sidecar 环境用；hex 仅传输编码，canonical key 为 raw）。
    pub fn secret_hex(&self) -> String {
        self.secret.iter().map(|b| format!("{b:02x}")).collect()
    }

    /// TTL（秒）。
    pub fn ttl_seconds(&self) -> i64 {
        self.ttl_seconds
    }
}

// ---------------------------------------------------------------------------
// 进程级共享密钥（供 sidecar spawn 注入 ST_HOSTFRAME_TOKEN_SECRET）
// ---------------------------------------------------------------------------

static SHARED_SECRET: OnceLock<Vec<u8>> = OnceLock::new();
static SHARED_TTL: OnceLock<i64> = OnceLock::new();

/// 安装共享密钥（重复安装返回 false：首次生效）。同时记录 TTL。
pub fn install_shared_secret(secret: Vec<u8>, ttl_seconds: i64) -> bool {
    let _ = SHARED_TTL.set(ttl_seconds);
    SHARED_SECRET.set(secret).is_ok()
}

/// 读取共享密钥（未安装 → None）。
pub fn shared_secret() -> Option<&'static [u8]> {
    SHARED_SECRET.get().map(|v| v.as_slice())
}

/// 密钥十六进制（供 spawn 注入；未安装 → None）。
pub fn shared_secret_hex() -> Option<String> {
    shared_secret().map(|s| s.iter().map(|b| format!("{b:02x}")).collect())
}

/// 共享 TTL（未安装 → 默认）。
pub fn shared_ttl_seconds() -> i64 {
    *SHARED_TTL.get().unwrap_or(&DEFAULT_HOST_FRAME_TTL_SECONDS)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn b64url_and_hmac_are_reused_from_import_token() {
        assert_eq!(b64url(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn token_prefix_is_sthost1() {
        let token = mint_host_frame_token(b"k", "s", "f.png", &"a".repeat(64), 1, 1, 0, 60, "n");
        assert!(token.starts_with("sthost1."));
    }

    /// 跨语言固定向量：与 Python `mint_host_frame_token(bytes(range(32)),
    /// "sess-vector", "00112233445566778899aabbccddeeff.png", "aa"*32, 120, 40,
    /// ttl=300, now=1000, nonce="00112233445566778899aabbccddeeff")` 逐字一致。
    #[test]
    fn token_matches_python_vector() {
        let secret: Vec<u8> = (0u8..32).collect(); // == hex "000102...1f"
        let token = mint_host_frame_token(
            &secret,
            "sess-vector",
            "00112233445566778899aabbccddeeff.png",
            &"aa".repeat(32),
            120,
            40,
            1000,
            300,
            "00112233445566778899aabbccddeeff",
        );
        assert_eq!(
            token,
            "sthost1.eyJ2IjoxLCJzaWQiOiJzZXNzLXZlY3RvciIsImYiOiIwMDExMjIzMzQ0NTU2Njc3ODg5OWFhYmJjY2RkZWVmZi5wbmciLCJzaGEiOiJhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhIiwidyI6MTIwLCJoIjo0MCwibiI6IjAwMTEyMjMzNDQ1NTY2Nzc4ODk5YWFiYmNjZGRlZWZmIiwiZXhwIjoxMzAwfQ.e2wvqqG8LwSkk2HjVZKuNF2VRbjux1AHyktjEiTbybA"
        );
    }

    #[test]
    fn temp_frame_roundtrip() {
        let base = std::env::temp_dir().join(format!("st-hf-{}", uuid::Uuid::new_v4()));
        let path = write_temp_frame(&base, "x.png", b"hello").expect("写入临时帧");
        assert_eq!(std::fs::read(&path).unwrap(), b"hello");
        assert!(path.starts_with(temp_dir_for(&base)));
        remove_temp_frame(&base, "x.png");
        assert!(!path.exists());
        let _ = std::fs::remove_dir_all(&base);
    }
}
