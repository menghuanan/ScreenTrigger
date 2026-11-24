# -*- coding: utf-8 -*-
"""TASK-009 DEF-009-1 回归：导入 token 的密钥表示（hex 传输 → raw 32 字节签名）。

独立覆盖两件事：

1. **跨语言共享 fixed vector**：与 Rust ``token.rs::token_matches_shared_fixed_vector``
   断言**同一** token 常量 —— 任一侧密钥/序列化表示漂移都会使该用例失败；
2. **生产装配路径**（直击缺陷）：按生产方式从 ``ST_IMPORT_TOKEN_SECRET``（hex）构造
   校验器（即 ``bytes.fromhex`` 路径）——消费用该 secret **raw 字节**签发的 token 必须
   通过；用不同密钥 / 用 hex 文本字节签发的 token 必须拒绝。修复前本用例失败（两侧
   HMAC key 表示不同：Rust raw 32 字节 vs Python hex 字符串的 64 个 UTF-8 字节）。

另覆盖容错：环境值缺失 / 非十六进制 / 长度≠32 字节 → 回落 ``NullTokenAuthority``
（诚实拒绝，不崩溃、不静默放行）并记一条 stderr 诊断。
"""

from __future__ import annotations

import io
import unittest

from backend.capture.errors import CaptureError
from backend.image.tokens import HmacTokenAuthority, NullTokenAuthority, mint_import_token
from backend.main import _build_import_token_authority

# --- 共享 fixed vector（必须与 Rust token.rs 逐字相同） ---
FIXED_SECRET_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
FIXED_SESSION = "sess-vector"
FIXED_PATH = "C:/pick/img.png"
FIXED_NOW = 1000
FIXED_TTL = 300
FIXED_NONCE = "00112233445566778899aabbccddeeff"
FIXED_TOKEN = (
    "stimp1.eyJ2IjoxLCJzaWQiOiJzZXNzLXZlY3RvciIsInBiIjoiUXpvdmNHbGpheTlwYldjdWNHNW4i"
    "LCJuIjoiMDAxMTIyMzM0NDU1NjY3Nzg4OTlhYWJiY2NkZGVlZmYiLCJleHAiOjEzMDB9."
    "xSUh0iQA5etMi19JXpG-3zJDgE8fOHdVY8JqPJngEx8"
)


class SharedVectorTest(unittest.TestCase):
    def test_python_token_equals_rust_constant(self) -> None:
        token = mint_import_token(
            bytes.fromhex(FIXED_SECRET_HEX),
            FIXED_SESSION,
            FIXED_PATH,
            ttl_seconds=FIXED_TTL,
            now=FIXED_NOW,
            nonce=FIXED_NONCE,
        )
        self.assertEqual(token, FIXED_TOKEN, "与 Rust 共享 fixed vector 不一致（表示漂移）")

    def test_full_vector_token_round_trips_through_consumer(self) -> None:
        # 共享 fixed vector token（exp=1300）在 now=1000 的注入时钟下应被消费通过；
        # 密钥表示（raw 32 字节）与签发侧一致时方可自证。
        auth = HmacTokenAuthority(bytes.fromhex(FIXED_SECRET_HEX), clock=lambda: 1000.0)
        resolved = auth.consume(FIXED_TOKEN, FIXED_SESSION)
        self.assertEqual(resolved.path, FIXED_PATH)


class ProductionAssemblyTest(unittest.TestCase):
    """生产方式（hex → raw 32 字节）装配的校验器必须接受 Rust 语义 token。"""

    def test_hex_secret_accepts_raw_signed_token(self) -> None:
        auth = _build_import_token_authority(FIXED_SECRET_HEX)
        self.assertIsInstance(auth, HmacTokenAuthority)
        # 宿主（Rust）用 raw 32 字节签名
        token = mint_import_token(
            bytes.fromhex(FIXED_SECRET_HEX), FIXED_SESSION, FIXED_PATH, nonce="n-ok"
        )
        resolved = auth.consume(token, FIXED_SESSION)
        self.assertEqual(resolved.path, FIXED_PATH)

    def test_hex_secret_rejects_token_signed_with_hex_text_key(self) -> None:
        # 反证：若（错误地）用 hex 字符串的 UTF-8 字节当 key 签发，生产装配必须拒绝
        auth = _build_import_token_authority(FIXED_SECRET_HEX)
        wrong = mint_import_token(
            FIXED_SECRET_HEX.encode("utf-8"), FIXED_SESSION, FIXED_PATH, nonce="n-bad"
        )
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(wrong, FIXED_SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_different_secret_rejected(self) -> None:
        auth = _build_import_token_authority(FIXED_SECRET_HEX)
        token = mint_import_token(
            bytes.fromhex("ff" * 32), FIXED_SESSION, FIXED_PATH, nonce="n-other"
        )
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, FIXED_SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


class SecretFallbackTest(unittest.TestCase):
    """非法/缺失环境值 → 诚实降级为 NullTokenAuthority（不崩溃、不静默放行）。"""

    def test_missing_secret_falls_back_to_null(self) -> None:
        log = io.StringIO()
        auth = _build_import_token_authority("", stderr=log)
        self.assertIsInstance(auth, NullTokenAuthority)
        self.assertIn("未注入", log.getvalue())

    def test_invalid_hex_falls_back_to_null(self) -> None:
        log = io.StringIO()
        auth = _build_import_token_authority("zzzz", stderr=log)
        self.assertIsInstance(auth, NullTokenAuthority)
        self.assertIn("非十六进制", log.getvalue())

    def test_wrong_length_falls_back_to_null(self) -> None:
        log = io.StringIO()
        auth = _build_import_token_authority("00112233", stderr=log)  # 4 字节 ≠32
        self.assertIsInstance(auth, NullTokenAuthority)
        self.assertIn("≠32", log.getvalue())

    def test_null_authority_rejects_any_token(self) -> None:
        auth = _build_import_token_authority("zzzz", stderr=io.StringIO())
        with self.assertRaises(CaptureError) as ctx:
            auth.consume("anything", FIXED_SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


if __name__ == "__main__":
    unittest.main()
