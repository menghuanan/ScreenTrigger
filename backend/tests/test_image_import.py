# -*- coding: utf-8 -*-
"""TASK-009：图片导入服务测试（token/校验/预算/方向/storage_mode/所有权/预览）。"""

from __future__ import annotations

import struct
import unittest
import zlib

from backend.capture.errors import CaptureError
from backend.image.formats import PNG_SIGNATURE
from backend.image.import_service import (
    MAX_EDGE,
    MAX_FILE_BYTES,
    MAX_PIXELS,
    ImageImportService,
)
from backend.image.png import encode_png_bgr
from backend.image.store import SessionImageStore
from backend.image.exif import read_orientation
from backend.image.tokens import HmacTokenAuthority, NullTokenAuthority, mint_import_token

from backend.tests._import_fakes import (
    FakeFileReader,
    FakeTokenAuthority,
    _chunk,
    corrupt_crc_png,
    jpeg_bytes,
    jpeg_with_orientation,
    make_token,
    pillow_available,
)

SECRET = b"unit-test-secret"


def _png(width: int = 4, height: int = 3) -> bytes:
    return encode_png_bgr(bytes(width * height * 3), width, height)


def _service(store, data: bytes, name: str = "a.png", **kwargs):
    path = "C:/pick/" + name
    authority = FakeTokenAuthority({"tok": path})
    reader = FakeFileReader({path: data})
    return ImageImportService(
        session_id="s1", store=store, token_authority=authority, file_reader=reader, **kwargs
    )


class ImportHappyPathTest(unittest.TestCase):
    def test_png_original(self) -> None:
        store = SessionImageStore(session_id="s1")
        svc = _service(store, _png(6, 4))
        image_ref, asset = svc.import_image("tok")
        self.assertEqual((image_ref.width, image_ref.height), (6, 4))
        self.assertEqual(image_ref.format, "png")
        self.assertEqual(len(image_ref.sha256), 64)
        self.assertEqual(asset.storage_mode, "original")
        self.assertTrue(asset.path.startswith("images/"))
        self.assertTrue(asset.path.endswith(".png"))
        self.assertIn(asset.sha256, asset.path)
        # 预览可用（复用既有 get_image_preview 路径）
        mime, _b64 = store.preview(image_ref.image_id, 200)
        self.assertEqual(mime, "image/png")
        # 释放幂等
        self.assertTrue(store.release(image_ref.image_id))
        self.assertTrue(store.release(image_ref.image_id))

    def test_png_lossless(self) -> None:
        store = SessionImageStore(session_id="s1")
        svc = _service(store, _png(5, 5), storage_mode="png_lossless")
        _ref, asset = svc.import_image("tok")
        self.assertEqual(asset.storage_mode, "png_lossless")
        self.assertEqual(asset.format, "png")
        self.assertTrue(asset.path.endswith(".png"))

    def test_asset_sha256_is_file_hash_for_original(self) -> None:
        import hashlib

        data = _png(3, 3)
        store = SessionImageStore(session_id="s1")
        svc = _service(store, data)
        _ref, asset = svc.import_image("tok")
        self.assertEqual(asset.sha256, hashlib.sha256(data).hexdigest())


class ImportValidationTest(unittest.TestCase):
    def _svc(self, data, name="a.png", **kw):
        return _service(SessionImageStore(session_id="s1"), data, name=name, **kw)

    def test_corrupt_png_rejected(self) -> None:
        svc = self._svc(corrupt_crc_png())
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_disguised_extension_rejected(self) -> None:
        # .png 内容却是 JPEG（伪装扩展名）
        if not pillow_available():
            self.skipTest("需要 Pillow 构造 JPEG")
        svc = self._svc(jpeg_bytes(4, 4), name="fake.png")
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_unknown_extension_rejected(self) -> None:
        svc = self._svc(_png(), name="a.bmp")
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_empty_file_rejected(self) -> None:
        svc = self._svc(b"")
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_file_too_large(self) -> None:
        svc = self._svc(b"x" * 200, max_file_bytes=100)
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "RESOURCE_LIMIT")

    def test_unreadable_file(self) -> None:
        authority = FakeTokenAuthority({"tok": "C:/pick/missing.png"})
        svc = ImageImportService(
            session_id="s1",
            store=SessionImageStore(session_id="s1"),
            token_authority=authority,
            file_reader=FakeFileReader({}),
        )
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")


class ImportBudgetTest(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual(MAX_FILE_BYTES, 50 * 1024 * 1024)
        self.assertEqual(MAX_PIXELS, 40_000_000)
        self.assertEqual(MAX_EDGE, 16384)

    def _head_only_png(self, width: int, height: int) -> bytes:
        # 头声明的尺寸；IDAT 可为无效（预算检查在解码前，先于解码拒绝）
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (
            PNG_SIGNATURE
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b""))
            + _chunk(b"IEND", b"")
        )

    def test_pixels_over_budget(self) -> None:
        svc = _service(SessionImageStore(session_id="s1"),
                       self._head_only_png(8000, 6000))  # 48,000,000 > 40M
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "RESOURCE_LIMIT")

    def test_edge_over_budget(self) -> None:
        svc = _service(SessionImageStore(session_id="s1"),
                       self._head_only_png(20000, 10))  # 宽 20000 > 16384
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "RESOURCE_LIMIT")


class ImportTokenTest(unittest.TestCase):
    def _authority(self, now=1000.0):
        return HmacTokenAuthority(SECRET, ttl_seconds=300, clock=lambda: now)

    def test_one_time(self) -> None:
        auth = self._authority()
        token = mint_import_token(SECRET, "s1", "C:/x/a.png", now=1000.0, nonce="n1")
        resolved = auth.consume(token, "s1")
        self.assertEqual(resolved.path, "C:/x/a.png")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, "s1")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_expired(self) -> None:
        auth = HmacTokenAuthority(SECRET, ttl_seconds=10, clock=lambda: 2000.0)
        token = mint_import_token(SECRET, "s1", "C:/x/a.png", ttl_seconds=10, now=1000.0, nonce="n2")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, "s1")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_cross_session(self) -> None:
        auth = self._authority()
        token = mint_import_token(SECRET, "other", "C:/x/a.png", now=1000.0, nonce="n3")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, "s1")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_forged_signature(self) -> None:
        auth = self._authority()
        good = mint_import_token(SECRET, "s1", "C:/x/a.png", now=1000.0, nonce="n4")
        forged = good[:-4] + ("0000" if not good.endswith("0000") else "1111")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(forged, "s1")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_wrong_secret(self) -> None:
        auth = self._authority()
        token = mint_import_token(b"other-secret", "s1", "C:/x/a.png", now=1000.0, nonce="n5")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, "s1")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_missing_token_is_invalid_params(self) -> None:
        svc = ImageImportService(
            session_id="s1",
            store=SessionImageStore(session_id="s1"),
            token_authority=HmacTokenAuthority(SECRET),
        )
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "import_token")

    def test_null_authority_rejects_all(self) -> None:
        # 未装配选择器：任何 token 都拒绝（不伪造可用，不接受任意路径）
        svc = ImageImportService(
            session_id="s1",
            store=SessionImageStore(session_id="s1"),
            token_authority=NullTokenAuthority(),
        )
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("anything")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_end_to_end_hmac_token(self) -> None:
        auth = self._authority()
        token = mint_import_token(SECRET, "s1", "C:/x/a.png", now=1000.0, nonce="n6")
        reader = FakeFileReader({"C:/x/a.png": _png(2, 2)})
        svc = ImageImportService(
            session_id="s1",
            store=SessionImageStore(session_id="s1"),
            token_authority=auth,
            file_reader=reader,
        )
        image_ref, _asset = svc.import_image(token)
        self.assertEqual(image_ref.format, "png")


class ImportOrientationTest(unittest.TestCase):
    def test_png_exif_orientation_applied(self) -> None:
        # 3x2 PNG + eXIf Orientation=6 → 派生图 2x3
        width, height = 3, 2
        bgr = bytes((i + 1) for i in range(width * height * 3))
        base = encode_png_bgr(bgr, width, height)
        tiff = (
            b"II" + struct.pack("<H", 42) + struct.pack("<I", 8)
            + struct.pack("<H", 1)
            + struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<H", 6) + b"\x00\x00"
            + struct.pack("<I", 0)
        )
        # 在 IHDR 后插入 eXIf chunk
        ihdr_end = 8 + (4 + 4 + 13 + 4)
        png = base[:ihdr_end] + _chunk(b"eXIf", tiff) + base[ihdr_end:]
        self.assertEqual(read_orientation(png, "png"), 6)
        svc = _service(SessionImageStore(session_id="s1"), png)
        image_ref, _asset = svc.import_image("tok")
        self.assertEqual((image_ref.width, image_ref.height), (2, 3))

    @unittest.skipUnless(pillow_available(), "需要 Pillow 构造带 EXIF 的 JPEG")
    def test_jpeg_orientation_applied(self) -> None:
        data = jpeg_with_orientation(6, 4, 6)  # Orientation=6 → 4x6
        self.assertEqual(read_orientation(data, "jpeg"), 6)
        svc = _service(SessionImageStore(session_id="s1"), data, name="a.jpg")
        image_ref, asset = svc.import_image("tok")
        self.assertEqual((image_ref.width, image_ref.height), (4, 6))
        self.assertEqual(image_ref.format, "jpeg")
        self.assertTrue(asset.path.endswith(".jpg"))

    @unittest.skipUnless(pillow_available(), "需要 Pillow")
    def test_jpeg_no_orientation(self) -> None:
        data = jpeg_bytes(7, 5)
        svc = _service(SessionImageStore(session_id="s1"), data, name="b.jpeg")
        image_ref, _asset = svc.import_image("tok")
        self.assertEqual((image_ref.width, image_ref.height), (7, 5))


class ImportOwnershipTest(unittest.TestCase):
    def test_release_cross_session(self) -> None:
        store = SessionImageStore(session_id="s1")
        svc = _service(store, _png(2, 2))
        image_ref, _asset = svc.import_image("tok")
        other = SessionImageStore(session_id="s2")
        # 另一个会话的 store 无法释放（未知引用）
        with self.assertRaises(CaptureError) as ctx:
            other.release(image_ref.image_id)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


class JpegBackendTest(unittest.TestCase):
    def _jpeg_with_valid_header(self, width: int, height: int) -> bytes:
        # 合法 SOI + SOF0 头（宽高可读、过预算）但无有效像素数据 → 到解码阶段才失败
        payload = (
            bytes([8]) + struct.pack(">HH", height, width) + b"\x03"
            + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        )
        seg = b"\xff\xc0" + struct.pack(">H", len(payload) + 2) + payload
        return b"\xff\xd8" + seg + b"\xff\xd9"

    @unittest.skipIf(pillow_available(), "仅在无 Pillow 环境验证降级")
    def test_jpeg_engine_unavailable_without_pillow(self) -> None:
        # 合法 JPEG 头（宽高可读、过预算）→ 到解码阶段；无 Pillow → 严格 ENGINE_UNAVAILABLE
        svc = _service(
            SessionImageStore(session_id="s1"),
            self._jpeg_with_valid_header(50, 25),
            name="a.jpg",
        )
        with self.assertRaises(CaptureError) as ctx:
            svc.import_image("tok")
        self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
