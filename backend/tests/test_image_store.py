# -*- coding: utf-8 -*-
"""TASK-007 会话级图像注册表单测（backend/image）。

覆盖：put/get 往返与 sha256 确定性、格式/缓冲校验、会话所有权与跨会话拒绝、
release 幂等与过期拒绝、字节预算 RESOURCE_LIMIT、预览 PNG 结构与 512KiB 上限、
max_edge 缩放、PNG 编码器 BGR↔RGB 正确性。全部用内存帧，不依赖屏幕。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.errors import CaptureError  # noqa: E402
from backend.capture.ports import RawImage  # noqa: E402
from backend.image.png import (  # noqa: E402
    PNG_SIGNATURE,
    PREVIEW_MAX_BYTES,
    downsample_bgr,
    encode_png_bgr,
)
from backend.image.store import SessionImageStore  # noqa: E402
from backend.types.region import validate_image_ref  # noqa: E402


def _image(width=12, height=10, bgr=(1, 2, 3)) -> RawImage:
    return RawImage(width=width, height=height, data=bytes(bgr) * (width * height))


def _parse_ihdr(png: bytes):
    """返回 PNG IHDR 的 (width, height, bit_depth, color_type)。"""
    assert png[:8] == PNG_SIGNATURE
    # 8 signature + 4 len + 4 tag(IHDR) + 13 data
    length = struct.unpack(">I", png[8:12])[0]
    assert png[12:16] == b"IHDR"
    assert length == 13
    width, height, depth, color_type = struct.unpack(">IIBB", png[16:26])
    return width, height, depth, color_type


class PutGetTest(unittest.TestCase):
    def setUp(self):
        self.store = SessionImageStore("s-1")

    def test_put_get_roundtrip_and_sha256(self):
        img = _image(12, 10)
        ref = self.store.put(img, "png")
        self.assertEqual(ref.width, 12)
        self.assertEqual(ref.height, 10)
        self.assertEqual(ref.format, "png")
        self.assertEqual(ref.sha256, hashlib.sha256(img.data).hexdigest())
        validate_image_ref(
            {"image_id": ref.image_id, "width": ref.width, "height": ref.height,
             "format": ref.format, "sha256": ref.sha256}
        )
        got = self.store.get(ref.image_id)
        self.assertEqual(got.data, img.data)
        self.assertEqual(got.owner, "s-1")

    def test_put_rejects_bad_format_and_buffer(self):
        with self.assertRaises(CaptureError) as ctx:
            self.store.put(_image(4, 4), "bmp")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")
        with self.assertRaises(CaptureError) as ctx:
            self.store.put(RawImage(width=4, height=4, data=b"\x00" * 5), "png")
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_get_unknown_is_expired(self):
        with self.assertRaises(CaptureError) as ctx:
            self.store.get("img-nope")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
        self.assertFalse(self.store.has("img-nope"))


class OwnershipReleaseTest(unittest.TestCase):
    def test_release_idempotent_same_session(self):
        store = SessionImageStore("s-1")
        ref = store.put(_image(4, 4), "png")
        self.assertTrue(store.release(ref.image_id))
        self.assertTrue(store.release(ref.image_id))  # 幂等
        with self.assertRaises(CaptureError) as ctx:
            store.get(ref.image_id)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_release_unknown_is_expired_not_silent_success(self):
        store = SessionImageStore("s-1")
        with self.assertRaises(CaptureError) as ctx:
            store.release("img-foreign")
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")

    def test_cross_session_reference_rejected(self):
        store_a = SessionImageStore("s-A")
        store_b = SessionImageStore("s-B")
        ref = store_a.put(_image(4, 4), "png")
        # 跨会话：B 会话既看不到（get）也不能释放（release），均 IMAGE_EXPIRED
        with self.assertRaises(CaptureError) as ctx:
            store_b.get(ref.image_id)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
        with self.assertRaises(CaptureError) as ctx:
            store_b.release(ref.image_id)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")
        # A 会话引用未被 B 的失败释放破坏
        self.assertTrue(store_a.has(ref.image_id))

    def test_close_releases_all(self):
        store = SessionImageStore("s-1")
        ref = store.put(_image(4, 4), "png")
        store.close()
        self.assertFalse(store.has(ref.image_id))
        self.assertEqual(store.bytes_used, 0)


class BudgetTest(unittest.TestCase):
    def test_budget_exceeded_resource_limit(self):
        # 单帧 4x4x3 = 48 字节；预算 50 → 第二帧超限
        store = SessionImageStore("s-1", budget_bytes=50)
        store.put(_image(4, 4), "png")
        with self.assertRaises(CaptureError) as ctx:
            store.put(_image(4, 4), "png")
        self.assertEqual(ctx.exception.code, "RESOURCE_LIMIT")
        self.assertEqual(store.bytes_used, 48)

    def test_release_frees_budget(self):
        store = SessionImageStore("s-1", budget_bytes=50)
        ref = store.put(_image(4, 4), "png")
        store.release(ref.image_id)
        self.assertEqual(store.bytes_used, 0)
        store.put(_image(4, 4), "png")  # 释放后可再次登记


class PreviewTest(unittest.TestCase):
    def test_preview_png_structure_and_limit(self):
        store = SessionImageStore("s-1")
        ref = store.put(_image(200, 100), "png")
        mime, b64 = store.preview(ref.image_id, 400)
        self.assertEqual(mime, "image/png")
        png = base64.b64decode(b64)
        self.assertLessEqual(len(png), PREVIEW_MAX_BYTES)
        width, height, depth, color = _parse_ihdr(png)
        self.assertEqual((depth, color), (8, 2))
        self.assertLessEqual(max(width, height), 400)

    def test_preview_downscales_to_max_edge(self):
        store = SessionImageStore("s-1")
        ref = store.put(_image(800, 400), "png")
        _mime, b64 = store.preview(ref.image_id, 100)
        width, height, _depth, _color = _parse_ihdr(base64.b64decode(b64))
        self.assertEqual(max(width, height), 100)
        self.assertEqual((width, height), (100, 50))

    def test_preview_expired_ref(self):
        store = SessionImageStore("s-1")
        with self.assertRaises(CaptureError) as ctx:
            store.preview("img-nope", 400)
        self.assertEqual(ctx.exception.code, "IMAGE_EXPIRED")


class PngEncoderTest(unittest.TestCase):
    def test_bgr_to_rgb_swap(self):
        # 2x1：像素0 BGR=(10,20,30) 像素1 BGR=(40,50,60)
        data = bytes([10, 20, 30, 40, 50, 60])
        png = encode_png_bgr(data, 2, 1)
        # 手工还原 zlib IDAT 的首个扫描行（filter=0 后 6 字节）
        import zlib

        pos = 8
        idat = b""
        while pos < len(png):
            length = struct.unpack(">I", png[pos:pos + 4])[0]
            tag = png[pos + 4:pos + 8]
            chunk = png[pos + 8:pos + 8 + length]
            if tag == b"IDAT":
                idat += chunk
            pos += 12 + length
        raw = zlib.decompress(idat)
        self.assertEqual(raw[0], 0)  # filter byte
        self.assertEqual(raw[1:7], bytes([30, 20, 10, 60, 50, 40]))  # RGB

    def test_downsample_no_upscale(self):
        data = bytes([1, 2, 3]) * 16  # 4x4
        same, w, h = downsample_bgr(data, 4, 4, 8)
        self.assertEqual((w, h), (4, 4))
        self.assertEqual(same, data)

    def test_encode_rejects_bad_size(self):
        with self.assertRaises(ValueError):
            encode_png_bgr(b"\x00" * 5, 2, 2)


if __name__ == "__main__":
    unittest.main()
