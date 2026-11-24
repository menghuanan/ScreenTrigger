# -*- coding: utf-8 -*-
"""最小 PNG 编码器（TASK-007，标准库 zlib 实现；零第三方依赖）。

输入为内部固定的 **BGR uint8** 行主序帧（02），输出为真彩 PNG（colortype=2）。
仅使用 ``zlib`` 与 ``struct``：不引入 Pillow/OpenCV，避免新增依赖与包体/许可证负担。

覆盖：
- ``encode_png_bgr``：BGR → 真彩 PNG（每扫描行 filter=0，BGR↔RGB 交换）；
- ``downsample_bgr``：盒式（区域平均）缩放，用于预览（max_edge 限制）；
- ``render_preview_png``：缩放到 max_edge 后编码，必要时逐级降边以满足 ≤512KiB。

JPEG 编码不在标准库内；本任务只产出 PNG（导入的 jpeg 属 TASK-009）。
"""

from __future__ import annotations

import struct
import zlib
from typing import Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PIXEL_CHANNELS = 3  # BGR / RGB 每像素 3 字节
PREVIEW_MAX_BYTES = 512 * 1024  # 03：get_image_preview 编码 ≤512KiB


def _chunk(tag: bytes, payload: bytes) -> bytes:
    """组一个 PNG chunk：长度 + 标签 + 载荷 + CRC32（标签+载荷）。"""
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png_bgr(data: bytes, width: int, height: int, compression_level: int = 6) -> bytes:
    """把 BGR uint8 帧编码为真彩 PNG 字节。

    Args:
        data: BGR 行主序字节，长度须为 ``width*height*3``。
        width: 像素宽（>0）。
        height: 像素高（>0）。
        compression_level: 0–9（config.capture.png_compression_level）。

    Raises:
        ValueError: 尺寸非法或缓冲区长度不匹配。
    """
    if width <= 0 or height <= 0:
        raise ValueError("width/height 必须为正")
    stride = width * PIXEL_CHANNELS
    if len(data) != stride * height:
        raise ValueError("BGR 缓冲区长度与 width*height*3 不符")

    # 逐行加 filter=0 前缀，并在同一趟把 BGR 交换为 RGB。
    raw = bytearray((stride + 1) * height)
    for row in range(height):
        src = row * stride
        dst = row * (stride + 1)
        raw[dst] = 0  # filter type: none
        line = data[src:src + stride]
        rgb = bytearray(stride)
        rgb[0::3] = line[2::3]  # R <- B (offset 2)
        rgb[1::3] = line[1::3]  # G <- G
        rgb[2::3] = line[0::3]  # B <- R (offset 0)
        raw[dst + 1:dst + 1 + stride] = rgb

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), compression_level)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def downsample_bgr(
    data: bytes, width: int, height: int, max_edge: int
) -> Tuple[bytes, int, int]:
    """把 BGR 帧等比缩放到最长边 ≤ ``max_edge``（盒式平均；不放大）。

    Returns:
        (缩放后字节, 新宽, 新高)；当无需缩放时原样返回 (data, width, height)。
    """
    if max_edge <= 0:
        raise ValueError("max_edge 必须为正")
    longest = max(width, height)
    if longest <= max_edge:
        return data, width, height

    new_w = max(1, int(round(width * max_edge / longest)))
    new_h = max(1, int(round(height * max_edge / longest)))
    if new_w == width and new_h == height:
        return data, width, height

    out = bytearray(new_w * new_h * PIXEL_CHANNELS)
    # 盒式平均：每个目标像素覆盖源区域 [x0,x1)×[y0,y1)。
    for ty in range(new_h):
        y0 = ty * height // new_h
        y1 = max(y0 + 1, (ty + 1) * height // new_h)
        for tx in range(new_w):
            x0 = tx * width // new_w
            x1 = max(x0 + 1, (tx + 1) * width // new_w)
            blue = green = red = 0
            count = (y1 - y0) * (x1 - x0)
            for sy in range(y0, y1):
                base = sy * width * PIXEL_CHANNELS
                for sx in range(x0, x1):
                    idx = base + sx * PIXEL_CHANNELS
                    blue += data[idx]
                    green += data[idx + 1]
                    red += data[idx + 2]
            di = (ty * new_w + tx) * PIXEL_CHANNELS
            out[di] = blue // count
            out[di + 1] = green // count
            out[di + 2] = red // count
    return bytes(out), new_w, new_h


def render_preview_png(
    data: bytes, width: int, height: int, max_edge: int, compression_level: int = 6
) -> Tuple[bytes, int, int]:
    """生成满足 ``≤512KiB`` 的预览 PNG（必要时逐级降低 max_edge 重编码）。

    Returns:
        (png 字节, 编码时使用的宽, 高)。
    """
    edge = max_edge
    while True:
        scaled, new_w, new_h = downsample_bgr(data, width, height, edge)
        png = encode_png_bgr(scaled, new_w, new_h, compression_level)
        if len(png) <= PREVIEW_MAX_BYTES or edge <= 1:
            return png, new_w, new_h
        edge = max(1, edge // 2)
