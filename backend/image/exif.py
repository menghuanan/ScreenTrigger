# -*- coding: utf-8 -*-
"""EXIF Orientation 读取（TASK-009，零第三方依赖）。

从 JPEG（APP1 Exif）或 PNG（eXIf chunk）中读取 TIFF 头里的 Orientation（tag
0x0112）。无法解析时返回 1（原样），**不抛错**——方向缺失不是坏图。
方向的应用（旋转/镜像）见 ``pixelops.apply_orientation``。
"""

from __future__ import annotations

import struct
from typing import Optional

from .png_decode import PNG_SIGNATURE

_ORIENTATION_TAG = 0x0112


def _read_tiff_orientation(tiff: bytes) -> Optional[int]:
    """解析 TIFF/EXIF 头，返回 Orientation（1..8）；解析失败返回 None。"""
    if len(tiff) < 8:
        return None
    byte_order = tiff[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return None
    (magic,) = struct.unpack(endian + "H", tiff[2:4])
    if magic != 42:
        return None
    (ifd0_offset,) = struct.unpack(endian + "I", tiff[4:8])
    pos = ifd0_offset
    if pos + 2 > len(tiff):
        return None
    (count,) = struct.unpack(endian + "H", tiff[pos:pos + 2])
    entry = pos + 2
    for _ in range(count):
        if entry + 12 > len(tiff):
            return None
        tag, field_type, _count_val = struct.unpack(
            endian + "HHI", tiff[entry:entry + 8]
        )
        if tag == _ORIENTATION_TAG:
            if field_type == 3:  # SHORT
                (value,) = struct.unpack(endian + "H", tiff[entry + 8:entry + 10])
                return value if 1 <= value <= 8 else None
            return None
        entry += 12
    return None


def _jpeg_exif_orientation(data: bytes) -> Optional[int]:
    """扫描 JPEG 段，取 APP1 Exif 的 TIFF 头。"""
    pos = 2
    total = len(data)
    while pos + 4 <= total:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker == 0xFF:
            pos -= 1
            continue
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 1 >= total:
            break
        (seg_len,) = struct.unpack(">H", data[pos:pos + 2])
        if seg_len < 2:
            return None
        if marker == 0xE1:  # APP1
            payload = data[pos + 2:pos + seg_len]
            if payload[:6] == b"Exif\x00\x00":
                return _read_tiff_orientation(payload[6:])
        pos += seg_len
    return None


def _png_exif_orientation(data: bytes) -> Optional[int]:
    """在 PNG chunk 中查找 eXIf。"""
    if len(data) < 8 or data[:8] != PNG_SIGNATURE:
        return None
    pos = 8
    total = len(data)
    while pos + 8 <= total:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body_start = pos + 8
        if body_start + length > total:
            return None
        if tag == b"eXIf":
            return _read_tiff_orientation(data[body_start:body_start + length])
        if tag == b"IEND":
            return None
        pos = body_start + length + 4
    return None


def read_orientation(data: bytes, image_format: str) -> int:
    """返回 EXIF Orientation（1..8）；无/解析失败返回 1。"""
    orientation: Optional[int] = None
    if image_format == "jpeg":
        orientation = _jpeg_exif_orientation(data)
    elif image_format == "png":
        orientation = _png_exif_orientation(data)
    return orientation if orientation is not None else 1
