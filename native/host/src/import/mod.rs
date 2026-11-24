//! 图片导入的会话绑定令牌。桌面文件选择器选定文件后，由宿主签发令牌。
//! 宿主以 ST_IMPORT_TOKEN_SECRET 向 Python 子进程提供本次运行的校验密钥。

pub mod token;

use std::sync::OnceLock;

/// 默认 TTL（秒）：选择器 → 导入的一次性窗口。
pub const DEFAULT_TTL_SECONDS: i64 = 300;

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
