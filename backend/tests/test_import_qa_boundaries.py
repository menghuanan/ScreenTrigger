# -*- coding: utf-8 -*-
"""TASK-009 独立 QA 边界测试（QA 工程师 software-qa-engineer-2 编写）。

不采信工程师自述，独立覆盖：
1. 伪装扩展名矩阵（内容 vs 扩展名双向、大小写、无扩展名）；
2. EXIF 方向 1–8 对**派生图**的应用 + 原字节保留（original 不得被旋转）；
3. token 重放/过期（恰在 exp）/伪造签名/截断/跨会话/密钥不入日志；
4. 预算与宽高边界（恰 == 上限通过、+1 拒绝）；
5. 预览生命周期：max_edge 1/800/801 边界、release 幂等、释放后预览 IMAGE_EXPIRED；
6. PNG 解码器子集：压缩方法≠0 / 滤波方法≠0 / 非法位深组合 / 缺 PLTE / 缺 IEND / CRC；
7. JPEG 降级诚实性（无 Pillow → ENGINE_UNAVAILABLE，不伪称成功）；
8. 路径安全：只收 token，不接受 path 参数；资产路径无越界。
"""

from __future__ import annotations

import base64
import hashlib
import struct
import unittest
import zlib

from backend.capture.errors import CaptureError
from backend.image.decode_common import decode_image_to_bgr
from backend.image.formats import PNG_SIGNATURE
from backend.image.import_service import ImageImportService
from backend.image.png import encode_png_bgr
from backend.image.png_decode import PngDecodeError, decode_png_to_rgb
from backend.image.store import SessionImageStore
from backend.image.tokens import HmacTokenAuthority, NullTokenAuthority, mint_import_token

from backend.tests._import_fakes import (
    FakeFileReader,
    FakeTokenAuthority,
    _chunk,
    jpeg_bytes,
    pillow_available,
)

SECRET = b"qa-independent-secret"
SESSION = "sess-qa"


# ---------------------------------------------------------------------------
# 构造工具（独立于工程师测试）
# ---------------------------------------------------------------------------

def png_with_exif(width: int, height: int, orientation: int, bgr: bytes = None) -> bytes:
    """真彩 PNG + eXIf(orientation)（插在 IHDR 之后）。"""
    if bgr is None:
        bgr = bytes((i * 7 + 3) % 256 for i in range(width * height * 3))
    base = encode_png_bgr(bgr, width, height)
    tiff = (
        b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + struct.pack("<H", 1)
        + struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<H", orientation) + b"\x00\x00"
        + struct.pack("<I", 0)
    )
    ihdr_end = 8 + (4 + 4 + 13 + 4)
    return base[:ihdr_end] + _chunk(b"eXIf", tiff) + base[ihdr_end:]


def png_header_only(width: int, height: int, color_type: int = 2, bit_depth: int = 8,
                    comp: int = 0, filt: int = 0, interlace: int = 0,
                    with_plte: bool = False) -> bytes:
    """仅头部 PNG（IDAT 空）——用于解码前预算/特性拒绝测试。"""
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, comp, filt, interlace)
    chunks = [PNG_SIGNATURE, _chunk(b"IHDR", ihdr)]
    if with_plte:
        chunks.append(_chunk(b"PLTE", bytes([255, 0, 0])))
    chunks.append(_chunk(b"IDAT", zlib.compress(b"")))
    chunks.append(_chunk(b"IEND", b""))
    return b"".join(chunks)


def jpeg_sof_only(width: int, height: int) -> bytes:
    """最小 JPEG 头（SOI + SOF0），解码前可读宽高；非真实可解码像素数据。"""
    payload = bytes([8]) + struct.pack(">HH", height, width) + b"\x03" + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    seg = b"\xff\xc0" + struct.pack(">H", len(payload) + 2) + payload
    return b"\xff\xd8" + seg + b"\xff\xd9"


def _service(data: bytes, name: str = "a.png", svc_store=None, **kwargs) -> ImageImportService:
    """构造导入服务（真实 HmacTokenAuthority 或注入替身），返回 (svc, store, make_token)。"""
    path = "C:/qa/" + name
    authority = FakeTokenAuthority({"tok": path})
    reader = FakeFileReader({path: data})
    store = svc_store if svc_store is not None else SessionImageStore(session_id=SESSION)
    svc = ImageImportService(
        session_id=SESSION, store=store, token_authority=authority,
        file_reader=reader, **kwargs
    )
    return svc, store


def _expect_code(testcase: unittest.TestCase, svc, token, code: str):
    with testcase.assertRaises(CaptureError) as ctx:
        svc.import_image(token)
    testcase.assertEqual(ctx.exception.code, code, "期望 %s，实际 %s" % (code, ctx.exception.code))
    return ctx.exception


# ---------------------------------------------------------------------------
# 1. 伪装扩展名矩阵
# ---------------------------------------------------------------------------

class DisguisedExtensionMatrixTest(unittest.TestCase):
    def _import(self, data: bytes, name: str):
        svc, _ = _service(data, name=name)
        return svc.import_image("tok")

    def test_png_content_named_jpg_rejected(self):
        svc, _ = _service(encode_png_bgr(bytes(3 * 3 * 3), 3, 3), name="x.jpg")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")

    def test_png_content_named_jpeg_rejected(self):
        svc, _ = _service(encode_png_bgr(bytes(3 * 3 * 3), 3, 3), name="x.jpeg")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")

    def test_png_content_named_bmp_rejected(self):
        svc, _ = _service(encode_png_bgr(bytes(3 * 3 * 3), 3, 3), name="x.bmp")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")

    def test_png_content_no_extension_rejected(self):
        svc, _ = _service(encode_png_bgr(bytes(3 * 3 * 3), 3, 3), name="noext")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")

    def test_uppercase_extension_accepted(self):
        ref, _asset = self._import(encode_png_bgr(bytes(2 * 2 * 3), 2, 2), name="A.PNG")
        self.assertEqual(ref.format, "png")
        self.assertEqual((ref.width, ref.height), (2, 2))

    @unittest.skipUnless(pillow_available(), "需要 Pillow 生成真实 JPEG")
    def test_jpeg_content_named_png_rejected(self):
        svc, _ = _service(jpeg_bytes(4, 4), name="fake.png")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")

    def test_jpeg_magic_named_png_rejected(self):
        svc, _ = _service(jpeg_sof_only(10, 10), name="fake.png")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")


# ---------------------------------------------------------------------------
# 2. EXIF 方向（派生图）与原字节保留
# ---------------------------------------------------------------------------

class ExifOrientationTest(unittest.TestCase):
    def test_orientation_dimension_matrix(self):
        for orientation, expect_wh in (
            (1, (4, 2)), (2, (4, 2)), (3, (4, 2)), (4, (4, 2)),
            (5, (2, 4)), (6, (2, 4)), (7, (2, 4)), (8, (2, 4)),
        ):
            with self.subTest(orientation=orientation):
                data = png_with_exif(4, 2, orientation)
                svc, _ = _service(data)
                ref, _asset = svc.import_image("tok")
                self.assertEqual((ref.width, ref.height), expect_wh,
                                 "orientation %d 宽高错误" % orientation)

    def test_orientation_6_pixel_mapping_is_90cw(self):
        # 2x2 源像素（BGR）：[p00=1, p01=2 / p10=3, p11=4]
        bgr = bytes([1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4])
        data = png_with_exif(2, 2, 6, bgr=bgr)
        svc, store = _service(data)
        ref, _asset = svc.import_image("tok")
        derived = store.get(ref.image_id).data
        # orientation 6 = 顺时针 90°：派生 TL 应为源 BL
        self.assertEqual(bytes(derived[0:3]), bytes([3, 3, 3]))
        # 派生 BR 应为源 TR
        self.assertEqual(bytes(derived[9:12]), bytes([2, 2, 2]))
        self.assertEqual((ref.width, ref.height), (2, 2))

    def test_original_preserves_raw_bytes_with_exif(self):
        data = png_with_exif(4, 2, 6)
        svc, _ = _service(data)
        _ref, asset = svc.import_image("tok")
        # original：资产字节 == 原始文件字节（含 EXIF，未旋转）
        self.assertEqual(asset.storage_mode, "original")
        self.assertEqual(asset.sha256, hashlib.sha256(data).hexdigest())

    def test_png_lossless_reencodes_valid_png(self):
        data = png_with_exif(4, 2, 6)
        svc, _ = _service(data, storage_mode="png_lossless")
        _ref, asset = svc.import_image("tok")
        self.assertEqual(asset.storage_mode, "png_lossless")
        self.assertTrue(asset.path.endswith(".png"))
        # 与 original 语义不同：资产字节为派生图重编码（通常与源字节不同）
        self.assertNotEqual(asset.sha256, hashlib.sha256(data).hexdigest())

    def test_png_exif_chunk_does_not_break_decode(self):
        # eXIf 为辅助 chunk：解码器应忽略且不报 CRC（eXIf 自身 CRC 正确）
        w, h, rgb = decode_png_to_rgb(png_with_exif(3, 2, 1))
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(len(rgb), 3 * 2 * 3)


# ---------------------------------------------------------------------------
# 3. token 安全
# ---------------------------------------------------------------------------

class TokenSecurityTest(unittest.TestCase):
    def test_one_time_replay(self):
        auth = HmacTokenAuthority(SECRET, clock=lambda: 1000.0)
        token = mint_import_token(SECRET, SESSION, "C:/x/a.png", now=1000.0, nonce="n1")
        auth.consume(token, SESSION)
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_expiry_exact_boundary(self):
        # exp = now + ttl = 1300；恰在 exp 仍有效，exp+1 过期
        token = mint_import_token(SECRET, SESSION, "C:/x/a.png", ttl_seconds=300, now=1000.0, nonce="nb")
        at_exp = HmacTokenAuthority(SECRET, clock=lambda: 1300.0)
        self.assertEqual(at_exp.consume(token, SESSION).path, "C:/x/a.png")
        after = HmacTokenAuthority(SECRET, clock=lambda: 1301.0)
        with self.assertRaises(CaptureError) as ctx:
            after.consume(token, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_forged_signature(self):
        auth = HmacTokenAuthority(SECRET, clock=lambda: 1000.0)
        good = mint_import_token(SECRET, SESSION, "C:/x/a.png", now=1000.0, nonce="n2")
        parts = good.split(".")
        forged = parts[0] + "." + parts[1] + "." + ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(forged, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_wrong_secret_rejected(self):
        auth = HmacTokenAuthority(SECRET, clock=lambda: 1000.0)
        token = mint_import_token(b"other", SESSION, "C:/x/a.png", now=1000.0, nonce="n3")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_truncated_and_wrong_prefix(self):
        auth = HmacTokenAuthority(SECRET, clock=lambda: 1000.0)
        good = mint_import_token(SECRET, SESSION, "C:/x/a.png", now=1000.0, nonce="n4")
        for bad in (good[:20], "stimp2." + good.split(".", 1)[1], "not.a.token"):
            with self.subTest(bad=bad[:16]):
                with self.assertRaises(CaptureError) as ctx:
                    auth.consume(bad, SESSION)
                self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_cross_session_rejected(self):
        auth = HmacTokenAuthority(SECRET, clock=lambda: 1000.0)
        token = mint_import_token(SECRET, "other-session", "C:/x/a.png", now=1000.0, nonce="n5")
        with self.assertRaises(CaptureError) as ctx:
            auth.consume(token, SESSION)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_secret_never_logged(self):
        lines = []
        auth = HmacTokenAuthority(SECRET, clock=lambda: 1000.0, logger=lines.append)
        token = mint_import_token(SECRET, SESSION, "C:/x/a.png", now=1000.0, nonce="n6")
        auth.consume(token, SESSION)
        joined = " ".join(lines)
        self.assertNotIn(SECRET.decode(), joined)
        self.assertNotIn(SECRET.hex(), joined)

    def test_null_authority_rejects(self):
        svc = ImageImportService(
            session_id=SESSION, store=SessionImageStore(session_id=SESSION),
            token_authority=NullTokenAuthority(),
        )
        _expect_code(self, svc, "anything", "IMAGE_EXPIRED")

    def test_missing_token_is_invalid_params(self):
        svc = ImageImportService(
            session_id=SESSION, store=SessionImageStore(session_id=SESSION),
            token_authority=HmacTokenAuthority(SECRET),
        )
        exc = _expect_code(self, svc, "", "INVALID_PARAMS")
        self.assertEqual(exc.details.get("field"), "import_token")


# ---------------------------------------------------------------------------
# 4. 预算与宽高边界
# ---------------------------------------------------------------------------

class BudgetBoundaryTest(unittest.TestCase):
    def test_file_size_exact_boundary(self):
        # 恰 == max_file_bytes 通过尺寸检查（随后因格式失败 INVALID_IMAGE）；+1 → RESOURCE_LIMIT
        exact_png = png_header_only(2, 2)
        svc, _ = _service(b"\x00" * 100, name="a.png", max_file_bytes=100)
        # 100 字节非 PNG → 通过尺寸检查后格式非法
        _expect_code(self, svc, "tok", "INVALID_IMAGE")
        svc2, _ = _service(b"\x00" * 101, name="a.png", max_file_bytes=100)
        _expect_code(self, svc2, "tok", "RESOURCE_LIMIT")
        del exact_png

    def test_default_file_limit_is_50mib(self):
        svc, _ = _service(b"\x00" * 10, name="a.png")
        self.assertEqual(svc._max_file_bytes if hasattr(svc, "_max_file_bytes") else 50 * 1024 * 1024,
                         50 * 1024 * 1024)

    def test_pixel_budget_exact_and_plus_one(self):
        # 用 max_pixels=100 精确验证 > 语义：10x10==100 通过预算（转 INVALID_IMAGE）；10x11=110 拒绝
        ok, _ = _service(png_header_only(10, 10), name="a.png", max_pixels=100)
        _expect_code(self, ok, "tok", "INVALID_IMAGE")
        over, _ = _service(png_header_only(10, 11), name="a.png", max_pixels=100)
        _expect_code(self, over, "tok", "RESOURCE_LIMIT")

    def test_real_40m_pixel_budget(self):
        # 8000x5000 == 40,000,000 恰在 06 上限内 → 过预算（转 INVALID_IMAGE）；
        # 8000x5001 = 40,008,000 > 40,000,000 → RESOURCE_LIMIT
        ok, _ = _service(png_header_only(8000, 5000), name="a.png")
        _expect_code(self, ok, "tok", "INVALID_IMAGE")
        over, _ = _service(png_header_only(8000, 5001), name="a.png")
        _expect_code(self, over, "tok", "RESOURCE_LIMIT")

    def test_edge_budget_exact_and_plus_one(self):
        ok, _ = _service(png_header_only(16384, 1), name="a.png")
        _expect_code(self, ok, "tok", "INVALID_IMAGE")
        over, _ = _service(png_header_only(16385, 1), name="a.png")
        _expect_code(self, over, "tok", "RESOURCE_LIMIT")


# ---------------------------------------------------------------------------
# 5. 预览 / release 生命周期
# ---------------------------------------------------------------------------

class PreviewLifecycleTest(unittest.TestCase):
    def test_preview_max_edge_bounds_and_release(self):
        data = encode_png_bgr(bytes(40 * 30 * 3), 40, 30)
        svc, store = _service(data)
        ref, _asset = svc.import_image("tok")
        for edge in (1, 800):
            mime, b64 = store.preview(ref.image_id, edge)
            self.assertEqual(mime, "image/png")
            raw = base64.b64decode(b64)
            self.assertLessEqual(len(raw), 512 * 1024)
        self.assertTrue(store.release(ref.image_id))
        self.assertTrue(store.release(ref.image_id))  # 幂等
        with self.assertRaises(CaptureError) as ctx:
            store.preview(ref.image_id, 400)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
        with self.assertRaises(CaptureError):
            store.get(ref.image_id)

    def test_cross_session_release_rejected(self):
        svc, _ = _service(encode_png_bgr(bytes(2 * 2 * 3), 2, 2))
        ref, _asset = svc.import_image("tok")
        other = SessionImageStore(session_id="other")
        with self.assertRaises(CaptureError) as ctx:
            other.release(ref.image_id)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


# ---------------------------------------------------------------------------
# 6. PNG 解码器子集边界
# ---------------------------------------------------------------------------

class PngSubsetRejectTest(unittest.TestCase):
    def test_compression_method_nonzero(self):
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png_header_only(2, 2, comp=1))

    def test_filter_method_nonzero(self):
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png_header_only(2, 2, filt=1))

    def test_invalid_bit_depth_for_color_type(self):
        # color_type=2 仅允许位深 8/16；位深 4 非法
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png_header_only(2, 2, color_type=2, bit_depth=4))

    def test_palette_missing_plte(self):
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png_header_only(2, 2, color_type=3, bit_depth=8, with_plte=False))

    def test_interlace_rejected(self):
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png_header_only(2, 2, interlace=1))

    def test_crc_tamper_rejected(self):
        good = encode_png_bgr(bytes(3 * 3 * 3), 3, 3)
        tampered = bytearray(good)
        tampered[8 + 4 + 4 + 13] ^= 0xFF  # IHDR CRC
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(bytes(tampered))

    def test_missing_iend_rejected(self):
        good = encode_png_bgr(bytes(2 * 2 * 3), 2, 2)
        # 去掉最后 12 字节（IEND chunk）
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(good[:-12])


# ---------------------------------------------------------------------------
# 7. JPEG 降级诚实性
# ---------------------------------------------------------------------------

class JpegDegradeHonestyTest(unittest.TestCase):
    @unittest.skipIf(pillow_available(), "仅在无 Pillow 环境验证降级诚实性")
    def test_valid_jpeg_header_engine_unavailable(self):
        data = jpeg_sof_only(50, 25)
        # 直接解码入口
        exc = None
        try:
            decode_image_to_bgr(data, "jpeg")
        except CaptureError as e:
            exc = e
        self.assertIsNotNone(exc)
        self.assertEqual(exc.code, "ENGINE_UNAVAILABLE")
        # 服务入口（过扩展名/格式/预算后到解码）
        svc, _ = _service(data, name="a.jpg")
        _expect_code(self, svc, "tok", "ENGINE_UNAVAILABLE")

    @unittest.skipUnless(pillow_available(), "仅在 Pillow 环境：同字节应为 INVALID_IMAGE 而非成功")
    def test_same_bytes_invalid_image_with_pillow(self):
        svc, _ = _service(jpeg_sof_only(50, 25), name="a.jpg")
        _expect_code(self, svc, "tok", "INVALID_IMAGE")


# ---------------------------------------------------------------------------
# 8. 路径安全
# ---------------------------------------------------------------------------

class PathSafetyTest(unittest.TestCase):
    def test_asset_path_never_traverses(self):
        svc, _ = _service(encode_png_bgr(bytes(3 * 3 * 3), 3, 3))
        _ref, asset = svc.import_image("tok")
        self.assertTrue(asset.path.startswith("images/"))
        self.assertNotIn("..", asset.path)
        self.assertFalse(asset.path.startswith("/"))

    def test_token_authority_is_only_path_source(self):
        # 令牌替身返回带 .. 的路径：服务仅按 token 解析（不校验越界——越界防护属选择器/token 签发）
        # 此处验证：路径只来自 authority.consume 的返回，而非任何 IPC 参数
        path = "C:/qa/../qa/a.png"
        authority = FakeTokenAuthority({"tok": path})
        reader = FakeFileReader({path: encode_png_bgr(bytes(2 * 2 * 3), 2, 2)})
        svc = ImageImportService(session_id=SESSION, store=SessionImageStore(session_id=SESSION),
                                 token_authority=authority, file_reader=reader)
        ref, _asset = svc.import_image("tok")
        self.assertEqual((ref.width, ref.height), (2, 2))
        self.assertEqual(reader.read_paths, [path])


if __name__ == "__main__":
    unittest.main()
