# -*- coding: utf-8 -*-
"""标准库 PNG 解码器（TASK-009，零第三方依赖）。

只使用 ``zlib`` + ``struct``。解码为 **RGB uint8** 行主序缓冲（由 import_service
转 BGR 后登记）。覆盖常见组合：

- 颜色类型 0（灰度）/2（真彩）/3（调色板）/4（灰度+alpha）/6（真彩+alpha）；
- 位深 1/2/4/8/16（子字节位深仅用于灰度/调色板）；
- 滤波 0–4；校验每 chunk CRC。

**不支持** 逐行隔行（interlace=1）与压缩方法≠0，遇之抛 ``PngDecodeError``
（import_service 映射 INVALID_IMAGE）。所有异常统一为 ``PngDecodeError`。
"""

from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_VALID_DEPTHS = {
    0: (1, 2, 4, 8, 16),
    2: (8, 16),
    3: (1, 2, 4, 8),
    4: (8, 16),
    6: (8, 16),
}


class PngDecodeError(ValueError):
    """PNG 解码失败（签名/结构/CRC/不支持的特性）。"""


def _iter_chunks(data: bytes):
    """遍历 PNG chunk，逐块校验 CRC；只 yield (tag, body)。"""
    if len(data) < 8 or data[:8] != PNG_SIGNATURE:
        raise PngDecodeError("非 PNG 签名")
    pos = 8
    total = len(data)
    while pos + 8 <= total:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body_start = pos + 8
        body_end = body_start + length
        if body_end + 4 > total:
            raise PngDecodeError("chunk 截断: %s" % tag.decode("latin1"))
        body = data[body_start:body_end]
        (expected,) = struct.unpack(">I", data[body_end:body_end + 4])
        actual = zlib.crc32(tag + body) & 0xFFFFFFFF
        if actual != expected:
            raise PngDecodeError("chunk CRC 校验失败: %s" % tag.decode("latin1"))
        yield tag, body
        if tag == b"IEND":
            return
        pos = body_end + 4


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, height: int, row_bytes: int, bpp: int) -> List[bytearray]:
    """按 PNG 滤波规则还原扫描行；返回每行原始字节（去滤波）。"""
    rows: List[bytearray] = []
    prev = bytearray(row_bytes)
    pos = 0
    stride = row_bytes + 1
    for _row in range(height):
        if pos + stride > len(raw):
            raise PngDecodeError("字节流长度不足（滤波数据截断）")
        ftype = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + row_bytes])
        pos += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, row_bytes):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(row_bytes):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(row_bytes):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(row_bytes):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        else:
            raise PngDecodeError("未知滤波类型: %d" % ftype)
        rows.append(line)
        prev = line
    return rows


def _row_samples(row: bytes, count: int, bit_depth: int, maxval: int) -> List[int]:
    """把一行原始字节展开为 ``count`` 个样本；16 位取高字节（降到 8 位）。"""
    if bit_depth == 8:
        if len(row) < count:
            raise PngDecodeError("扫描行长度不足")
        return list(row[:count])
    if bit_depth == 16:
        if len(row) < count * 2:
            raise PngDecodeError("16 位扫描行长度不足")
        return [(row[i * 2] << 8 | row[i * 2 + 1]) >> 8 for i in range(count)]
    per = 8 // bit_depth
    out: List[int] = []
    for i in range(count):
        byte = row[i // per]
        shift = 8 - bit_depth * (i % per + 1)
        out.append((byte >> shift) & maxval)
    return out


def decode_png_to_rgb(data: bytes) -> Tuple[int, int, bytes]:
    """解码 PNG → (width, height, RGB 行主序 uint8)。

    Raises:
        PngDecodeError: 签名/结构/CRC 错误或不支持的特性（隔行、压缩方法≠0）。
    """
    ihdr: bytes = b""
    plte: bytes = b""
    idat_parts: List[bytes] = []
    seen_iend = False
    for tag, body in _iter_chunks(data):
        if tag == b"IHDR":
            ihdr = body
        elif tag == b"PLTE":
            plte = body
        elif tag == b"IDAT":
            idat_parts.append(body)
        elif tag == b"IEND":
            seen_iend = True
        # 其余辅助 chunk（gAMA/tEXt/eXIf/…）忽略
    if not seen_iend:
        raise PngDecodeError("缺少 IEND")
    if len(ihdr) != 13:
        raise PngDecodeError("IHDR 长度非法")
    width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if width <= 0 or height <= 0:
        raise PngDecodeError("宽高必须为正")
    if comp != 0:
        raise PngDecodeError("不支持的压缩方法: %d" % comp)
    if filt != 0:
        raise PngDecodeError("不支持的滤波方法: %d" % filt)
    if interlace != 0:
        raise PngDecodeError("不支持隔行 PNG（interlace=1）")
    if color_type not in _VALID_DEPTHS:
        raise PngDecodeError("不支持的颜色类型: %d" % color_type)
    if bit_depth not in _VALID_DEPTHS[color_type]:
        raise PngDecodeError("颜色类型 %d 不支持位深 %d" % (color_type, bit_depth))
    if color_type == 3 and not plte:
        raise PngDecodeError("调色板图像缺少 PLTE")
    if not idat_parts:
        raise PngDecodeError("缺少 IDAT")

    channels = _CHANNELS[color_type]
    bits_per_pixel = channels * bit_depth
    bpp = max(1, bits_per_pixel // 8)  # 滤波字节步长（至少 1）
    row_bytes = (width * bits_per_pixel + 7) // 8

    try:
        raw = zlib.decompress(b"".join(idat_parts))
    except zlib.error as exc:
        raise PngDecodeError("IDAT 解压失败: %s" % exc) from exc

    rows = _unfilter(raw, height, row_bytes, bpp)
    maxval = (1 << bit_depth) - 1

    palette: List[Tuple[int, int, int]] = []
    if color_type == 3:
        if len(plte) % 3 != 0 or len(plte) < 3:
            raise PngDecodeError("PLTE 长度非法")
        palette = [
            (plte[i], plte[i + 1], plte[i + 2]) for i in range(0, len(plte), 3)
        ]

    out = bytearray(width * height * 3)
    o = 0
    sample_count = width * channels
    for y in range(height):
        samples = _row_samples(bytes(rows[y]), sample_count, bit_depth, maxval)
        if color_type == 0:
            for v in samples:
                g = v * 255 // maxval if bit_depth < 8 else v
                out[o] = out[o + 1] = out[o + 2] = g
                o += 3
        elif color_type == 2:
            for x in range(width):
                out[o] = samples[3 * x]
                out[o + 1] = samples[3 * x + 1]
                out[o + 2] = samples[3 * x + 2]
                o += 3
        elif color_type == 3:
            for idx in samples:
                if idx >= len(palette):
                    raise PngDecodeError("调色板索引越界")
                r, g, b = palette[idx]
                out[o] = r
                out[o + 1] = g
                out[o + 2] = b
                o += 3
        elif color_type == 4:
            for x in range(width):
                g = samples[2 * x]  # 灰度样本（16 位已在 _row_samples 降为 8 位）
                out[o] = out[o + 1] = out[o + 2] = g
                o += 3
        else:  # color_type == 6
            for x in range(width):
                out[o] = samples[4 * x]
                out[o + 1] = samples[4 * x + 1]
                out[o + 2] = samples[4 * x + 2]
                o += 3
    return width, height, bytes(out)
