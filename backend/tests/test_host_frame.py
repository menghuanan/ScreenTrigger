# -*- coding: utf-8 -*-
"""宿主帧登记契约测试（DEF-TASK008-01 修复，方向 A）。

覆盖：
- 跨语言固定向量（Rust ``hostframe::mint_host_frame_token`` 逐字一致的字符串）；
- token 安全：伪造签名 / 过期 / 跨会话 / 一次性重放 一律 IMAGE_EXPIRED；
- 完整性：sha256 不符 / 尺寸不符 → DATA_CORRUPT；缺文件 → IMAGE_EXPIRED；
- 路径安全（07 §6）：文件名含分离符/``..`` → INVALID_PARAMS；越界/链接逃逸拒绝；
- 成功登记：引擎 SessionImageStore 签发 ImageRef，``get``/预览/``release`` 幂等；
  临时文件登记后被删除；
- 命令接线：``register_host_frame`` 经 Dispatcher 返回 ``{image:ImageRef}``。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.capture.errors import CaptureError  # noqa: E402
from backend.image.host_frame import (  # noqa: E402
    HostFrameRegistrationService,
    HostFrameTokenAuthority,
    NullHostFrameAuthority,
    resolve_temp_file,
    temp_dir_for,
    mint_host_frame_token,
)
from backend.image.png import encode_png_bgr  # noqa: E402
from backend.image.store import SessionImageStore  # noqa: E402

SECRET = bytes(range(32))  # hex "000102...1f"
SESSION = "sess-vector"


def _png(width: int, height: int) -> bytes:
    return encode_png_bgr(bytes([10, 20, 30]) * (width * height), width, height)


class FixedVectorTest(unittest.TestCase):
    def test_token_matches_python_rust_fixed_vector(self):
        token = mint_host_frame_token(
            SECRET,
            SESSION,
            "00112233445566778899aabbccddeeff.png",
            "aa" * 32,
            120,
            40,
            ttl_seconds=300,
            now=1000,
            nonce="00112233445566778899aabbccddeeff",
        )
        # 与 Rust `hostframe::tests::token_matches_python_vector` 逐字一致。
        self.assertEqual(
            token,
            "sthost1.eyJ2IjoxLCJzaWQiOiJzZXNzLXZlY3RvciIsImYiOiIwMDExMjIzMzQ0NTU2Njc3ODg5OWFhYmJjY2RkZWVmZi5wbmciLCJzaGEiOiJhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhIiwidyI6MTIwLCJoIjo0MCwibiI6IjAwMTEyMjMzNDQ1NTY2Nzc4ODk5YWFiYmNjZGRlZWZmIiwiZXhwIjoxMzAwfQ.e2wvqqG8LwSkk2HjVZKuNF2VRbjux1AHyktjEiTbybA",
        )

    def test_authority_consumes_own_token(self):
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        token = mint_host_frame_token(
            SECRET, SESSION, "f.png", "aa" * 32, 8, 4, now=1000, nonce="n1"
        )
        resolved = auth.consume(token, SESSION)
        self.assertEqual(resolved.filename, "f.png")
        self.assertEqual((resolved.width, resolved.height), (8, 4))


class TokenSecurityTest(unittest.TestCase):
    def _token(self, **kw):
        base = dict(
            secret=SECRET,
            session_id=SESSION,
            filename="f.png",
            sha256="aa" * 32,
            width=8,
            height=4,
            ttl_seconds=300,
            now=1000,
            nonce="n-%d" % id(kw),
        )
        base.update(kw)
        return mint_host_frame_token(**base)

    def test_forged_signature_rejected(self):
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        good = self._token()
        forged = good[:-4] + "AAAA"
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(forged, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_wrong_secret_rejected(self):
        auth = HostFrameTokenAuthority(b"x" * 32, clock=lambda: 1000.0)
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(self._token(), SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_expired_rejected(self):
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 9999.0)
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(self._token(now=1000, ttl_seconds=60), SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_cross_session_rejected(self):
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(self._token(), "other-session")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_replay_rejected(self):
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        token = self._token(nonce="replay-1")
        auth.consume(token, SESSION)
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_bad_prefix_rejected(self):
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        with self.assertRaises(CaptureError):
            auth.consume("stimp1.aaa.bbb", SESSION)

    def test_null_authority_rejects(self):
        with self.assertRaises(CaptureError) as ctx:
            NullHostFrameAuthority().consume(self._token(), SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


class PathContainmentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="st-hf-")
        os.makedirs(temp_dir_for(self._tmp), exist_ok=True)

    def test_separator_in_filename_rejected(self):
        with self.assertRaises(CaptureError) as ctx:
            resolve_temp_file(self._tmp, "a/b.png")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_parent_reference_rejected(self):
        with self.assertRaises(CaptureError) as ctx:
            resolve_temp_file(self._tmp, "../escape.png")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_valid_filename_resolves_inside_temp(self):
        resolved = resolve_temp_file(self._tmp, "ok.png")
        self.assertTrue(resolved.startswith(os.path.realpath(temp_dir_for(self._tmp))))

    def test_symlink_escape_rejected(self):
        temp_root = temp_dir_for(self._tmp)
        outside = os.path.join(self._tmp, "outside.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("x")
        link = os.path.join(temp_root, "link.png")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不支持创建符号链接（需管理员/开发者模式）")
        if not os.path.islink(link):
            # 例如 MSYS/Git Bash 下 os.symlink 退化为「复制」，OS 不识别为链接：
            # 无真实链接可测 → 跳过（真实链接逃逸由 readlink/realpath+islink 兜底）。
            self.skipTest("当前环境 os.symlink 未产生真实符号链接（仿真复制）")
        with self.assertRaises(CaptureError) as ctx:
            resolve_temp_file(self._tmp, "link.png")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="st-hf-")
        self._temp_root = temp_dir_for(self._tmp)
        os.makedirs(self._temp_root, exist_ok=True)
        self._store = SessionImageStore(session_id=SESSION)
        self._auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        self._service = HostFrameRegistrationService(self._store, self._auth, self._tmp)

    def _stage(self, width=8, height=4, name="frame.png"):
        png = _png(width, height)
        path = os.path.join(self._temp_root, name)
        with open(path, "wb") as handle:
            handle.write(png)
        token = mint_host_frame_token(
            SECRET, SESSION, name, hashlib.sha256(png).hexdigest(), width, height,
            now=1000, nonce="reg-%s" % name,
        )
        return path, token

    def test_success_registers_engine_image(self):
        path, token = self._stage()
        ref = self._service.register(token, SESSION)
        self.assertTrue(ref.image_id.startswith("img-"))
        # 引擎可解析（get/preview 语义）；临时文件已删除
        self.assertTrue(self._store.has(ref.image_id))
        self.assertFalse(os.path.exists(path))
        mime, _b64 = self._store.preview(ref.image_id, 400)
        self.assertEqual(mime, "image/png")
        # release 幂等
        self.assertTrue(self._store.release(ref.image_id))
        self.assertTrue(self._store.release(ref.image_id))
        self.assertFalse(self._store.has(ref.image_id))

    def test_sha_mismatch_is_data_corrupt(self):
        path, _ = self._stage()
        png = _png(8, 4)
        token = mint_host_frame_token(
            SECRET, SESSION, "frame.png", "bb" * 32, 8, 4, now=1000, nonce="sha-bad"
        )
        with self.assertRaises(CaptureError) as ctx:
            self._service.register(token, SESSION)
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")
        self.assertTrue(os.path.exists(path))  # 校验失败不删除（由宿主回收）

    def test_dimension_mismatch_is_data_corrupt(self):
        _, _ = self._stage(width=8, height=4)
        png = _png(8, 4)
        token = mint_host_frame_token(
            SECRET, SESSION, "frame.png", hashlib.sha256(png).hexdigest(), 99, 4,
            now=1000, nonce="dim-bad",
        )
        with self.assertRaises(CaptureError) as ctx:
            self._service.register(token, SESSION)
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")

    def test_missing_file_is_image_expired(self):
        token = mint_host_frame_token(
            SECRET, SESSION, "absent.png", "aa" * 32, 8, 4, now=1000, nonce="miss"
        )
        with self.assertRaises(CaptureError) as ctx:
            self._service.register(token, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


class CommandWiringTest(unittest.TestCase):
    def test_register_host_frame_command_returns_engine_image_ref(self):
        from backend.ipc.envelope import encode_envelope  # noqa: F401
        from backend.ipc.dispatcher import Dispatcher

        tmp = tempfile.mkdtemp(prefix="st-hf-cmd-")
        temp_root = temp_dir_for(tmp)
        os.makedirs(temp_root, exist_ok=True)
        store = SessionImageStore(session_id=SESSION)
        auth = HostFrameTokenAuthority(SECRET, clock=lambda: 1000.0)
        service = HostFrameRegistrationService(store, auth, tmp)

        from backend.image.commands import register_host_frame_command

        class _Session:
            session_id = SESSION

        class _Transport:
            def __init__(self):
                self.sent = []

            def send(self, envelope):
                self.sent.append(envelope)
                return True

        transport = _Transport()
        dispatcher = Dispatcher(transport=transport, session=_Session())
        register_host_frame_command(dispatcher, service)

        png = _png(8, 4)
        with open(os.path.join(temp_root, "c.png"), "wb") as handle:
            handle.write(png)
        token = mint_host_frame_token(
            SECRET, SESSION, "c.png", hashlib.sha256(png).hexdigest(), 8, 4,
            now=1000, nonce="cmd-1",
        )
        dispatcher.dispatch_command(
            {
                "protocol_version": 1,
                "session_id": SESSION,
                "type": "request",
                "id": "host-frame-1",
                "command": "register_host_frame",
                "params": {"token": token},
            }
        )
        reply = transport.sent[-1]
        self.assertTrue(reply.get("success"), reply)
        image_id = reply["data"]["image"]["image_id"]
        self.assertTrue(image_id.startswith("img-"))
        self.assertTrue(store.has(image_id))


if __name__ == "__main__":
    unittest.main()
