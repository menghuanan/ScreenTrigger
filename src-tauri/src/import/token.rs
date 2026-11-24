//! 导入 token 的签发（TASK-009，M01 `import_image`）。
//!
//! 与 Python 侧 `backend/image/tokens.py:HmacTokenAuthority` **逐字同格式**：
//! `stimp1.<payload_b64>.<sig_b64>`，其中
//! - `payload = {"v":1,"sid":<session>,"pb":<base64url(path)>,"n":<nonce>,"exp":<epoch+ttl>}`
//!   （紧凑 JSON，字段顺序固定；path 经 base64url 编码使载荷恒为 ASCII，避免
//!   非 ASCII 路径在两种语言间 JSON 转义的差异）；
//! - `sig = base64url(hmac_sha256(secret, payload_b64))`（无填充，URL 安全字母表）。
//!
//! 一次性/过期/会话绑定由消费端（Python `HmacTokenAuthority`）强制；本模块只负责
//! **签发**。零新增 crate 之外的依赖：HMAC-SHA256 基于已有 `sha2`。

use serde::Serialize;
use sha2::{Digest, Sha256};

/// token 前缀（与 Python 侧 `TOKEN_PREFIX` 一致）。
pub const TOKEN_PREFIX: &str = "stimp1";

const B64_ALPHABET: &[u8; 64] =
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/// URL 安全 base64（无填充）。
pub fn b64url(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64_ALPHABET[((n >> 18) & 63) as usize] as char);
        out.push(B64_ALPHABET[((n >> 12) & 63) as usize] as char);
        if chunk.len() > 1 {
            out.push(B64_ALPHABET[((n >> 6) & 63) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(B64_ALPHABET[(n & 63) as usize] as char);
        }
    }
    out
}

/// HMAC-SHA256（RFC 2104），密钥/消息任意长度。
pub fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK: usize = 64;
    let mut k = [0u8; BLOCK];
    if key.len() > BLOCK {
        let digest = Sha256::digest(key);
        k[..32].copy_from_slice(&digest);
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut ipad = [0x36u8; BLOCK];
    let mut opad = [0x5cu8; BLOCK];
    for i in 0..BLOCK {
        ipad[i] ^= k[i];
        opad[i] ^= k[i];
    }
    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(message);
    let inner_digest = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_digest);
    let out = outer.finalize();
    let mut result = [0u8; 32];
    result.copy_from_slice(&out);
    result
}

/// 载荷字段顺序固定（serde 按结构体字段顺序序列化，与 Python dict 顺序一致）。
#[derive(Serialize)]
struct TokenPayload<'a> {
    v: u32,
    sid: &'a str,
    pb: String,
    n: &'a str,
    exp: i64,
}

/// 当前 epoch 秒。
pub fn now_epoch() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_secs() as i64,
        Err(_) => 0,
    }
}

/// 签发一次性导入 token（**仅供文件选择器**）。
pub fn mint_import_token(
    secret: &[u8],
    session_id: &str,
    path: &str,
    now: i64,
    ttl_seconds: i64,
    nonce: &str,
) -> String {
    let payload = TokenPayload {
        v: 1,
        sid: session_id,
        pb: b64url(path.as_bytes()),
        n: nonce,
        exp: now + ttl_seconds,
    };
    let json = serde_json::to_string(&payload).expect("TokenPayload 序列化不应失败");
    let payload_b64 = b64url(json.as_bytes());
    let signature = b64url(&hmac_sha256(secret, payload_b64.as_bytes()));
    format!("{TOKEN_PREFIX}.{payload_b64}.{signature}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn b64url_no_padding() {
        assert_eq!(b64url(b""), "");
        assert_eq!(b64url(b"f"), "Zg");
        assert_eq!(b64url(b"fo"), "Zm8");
        assert_eq!(b64url(b"foo"), "Zm9v");
        assert_eq!(b64url(b"foob"), "Zm9vYg");
        assert_eq!(b64url(b"fooba"), "Zm9vYmE");
        assert_eq!(b64url(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn hmac_sha256_rfc4231_case2() {
        // RFC 4231 Test Case 2：key="Jefe"，data="what do ya want for nothing?"
        let mac = hmac_sha256(b"Jefe", b"what do ya want for nothing?");
        let hex: String = mac.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(
            hex,
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
    }

    #[test]
    fn token_matches_python_vector() {
        // 固定向量：与 Python `mint_import_token(b"vector-secret","s1","C:/pick/a.png",
        // ttl=300, now=1000.0, nonce="deadbeef...")` 的产出逐字一致（跨语言自证）。
        let token = mint_import_token(
            b"vector-secret",
            "s1",
            "C:/pick/a.png",
            1000,
            300,
            "deadbeefdeadbeefdeadbeefdeadbeef",
        );
        assert_eq!(
            token,
            "stimp1.eyJ2IjoxLCJzaWQiOiJzMSIsInBiIjoiUXpvdmNHbGpheTloTG5CdVp3IiwibiI6ImRlYWRiZWVmZGVhZGJlZWZkZWFkYmVlZmRlYWRiZWVmIiwiZXhwIjoxMzAwfQ.Ke0CpirJn_mQycP75AiMzpBMZJp_1bJWYgUeNXcqAwg"
        );
    }

    /// 共享 fixed vector（密钥表示防漂移，DEF-009-1）：secret 以 **hex** 描述
    /// （`000102…1f`，32 字节）；签名 canonical key 为解码后的 **raw 字节**
    /// （即 `0u8..32`）。该常量与 Python 侧测试逐字相同，任一侧表示漂移都会失败。
    #[test]
    fn token_matches_shared_fixed_vector() {
        let secret: Vec<u8> = (0u8..32).collect(); // == hex "000102...1f"
        let token = mint_import_token(
            &secret,
            "sess-vector",
            "C:/pick/img.png",
            1000,
            300,
            "00112233445566778899aabbccddeeff",
        );
        assert_eq!(
            token,
            "stimp1.eyJ2IjoxLCJzaWQiOiJzZXNzLXZlY3RvciIsInBiIjoiUXpvdmNHbGpheTlwYldjdWNHNW4iLCJuIjoiMDAxMTIyMzM0NDU1NjY3Nzg4OTlhYWJiY2NkZGVlZmYiLCJleHAiOjEzMDB9.xSUh0iQA5etMi19JXpG-3zJDgE8fOHdVY8JqPJngEx8"
        );
    }
}
