# -*- coding: utf-8 -*-
"""TASK-009：标准库 PNG 解码器测试（零第三方依赖）。"""

from __future__ import annotations

import struct
import unittest
import zlib

from backend.image.formats import PNG_SIGNATURE, ImageFormatError, png_dimensions
from backend.image.png import encode_png_bgr
from backend.image.png_decode import PngDecodeError, decode_png_to_rgb

from backend.tests._import_fakes import _chunk, corrupt_crc_png, png_grayscale, png_palette, truncated_png


class PngRoundTripTest(unittest.TestCase):
    def test_truecolor_roundtrip(self) -> None:
        width, height = 5, 3
        bgr = bytes((i * 13 + 7) % 256 for i in range(width * height * 3))
        png = encode_png_bgr(bgr, width, height)
        w, h, rgb = decode_png_to_rgb(png)
        self.assertEqual((w, h), (width, height))
        # 期望的 RGB：BGR 交换
        expected = bytearray(bgr)
        expected[0::3], expected[2::3] = bgr[2::3], bgr[0::3]
        self.assertEqual(bytes(rgb), bytes(expected))

    def test_grayscale_8bit(self) -> None:
        gray = bytes([10, 20, 30, 40])
        png = png_grayscale(gray, 2, 2, bit_depth=8)
        w, h, rgb = decode_png_to_rgb(png)
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(bytes(rgb), bytes([10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40]))

    def test_grayscale_16bit_high_byte(self) -> None:
        gray = bytes([1, 2, 3, 4])
        png = png_grayscale(gray, 2, 2, bit_depth=16)
        w, h, rgb = decode_png_to_rgb(png)
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(bytes(rgb), bytes([1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4]))

    def test_palette(self) -> None:
        palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)]
        indices = bytes([0, 1, 2, 3])
        png = png_palette(indices, palette, 2, 2)
        w, h, rgb = decode_png_to_rgb(png)
        self.assertEqual((w, h), (2, 2))
        self.assertEqual(bytes(rgb), bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 10, 20, 30]))


class PngRejectTest(unittest.TestCase):
    def test_corrupt_crc(self) -> None:
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(corrupt_crc_png())

    def test_truncated(self) -> None:
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(truncated_png())

    def test_not_png(self) -> None:
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(b"\x00\x01\x02\x03not a png at all")

    def test_interlaced_rejected(self) -> None:
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)  # interlace=1
        raw = zlib.compress(b"\x00" + b"\x00" * 12)
        png = PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", raw) + _chunk(b"IEND", b"")
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png)

    def test_unknown_color_type_rejected(self) -> None:
        ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 7, 0, 0, 0)  # color_type=7 非法
        png = PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")
        with self.assertRaises(PngDecodeError):
            decode_png_to_rgb(png)


class PngHeaderTest(unittest.TestCase):
    def test_png_dimensions(self) -> None:
        png = encode_png_bgr(bytes(4 * 3 * 3), 4, 3)
        self.assertEqual(png_dimensions(png), (4, 3))

    def test_png_dimensions_bad(self) -> None:
        with self.assertRaises(ImageFormatError):
            png_dimensions(b"not png")


if __name__ == "__main__":
    unittest.main()
