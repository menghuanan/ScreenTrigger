# -*- coding: utf-8 -*-
"""像素缓冲工具（TASK-009）：RGB↔BGR 交换与 EXIF 方向应用。

内部图像固定 **BGR uint8** 行主序（02）。解码器产出 RGB，需要转 BGR 后登记；
导入派生图按 EXIF Orientation（02：识别派生图应用方向）旋转/翻转到规范方向。

方向值（EXIF Orientation 1..8）：
1 原样；2 水平镜像；3 旋转 180°；4 垂直镜像；
5 主对角转置；6 顺时针 90°；7 副对角转置；8 逆时针 90°。
"""

from __future__ import annotations

from typing import Tuple

CHANNELS = 3


def rgb_to_bgr(data: bytes, width: int, height: int) -> bytes:
    """RGB 行主序 → BGR 行主序（交换每像素的 R 与 B）。"""
    expected = width * height * CHANNELS
    if len(data) != expected:
        raise ValueError("RGB 缓冲区长度与 width*height*3 不符")
    out = bytearray(data)
    out[0::3] = data[2::3]  # B <- R
    out[2::3] = data[0::3]  # R <- B
    return bytes(out)


def bgr_to_rgb(data: bytes, width: int, height: int) -> bytes:
    """BGR 行主序 → RGB 行主序（与 rgb_to_bgr 互为逆）。"""
    return rgb_to_bgr(data, width, height)


def apply_orientation(
    data: bytes, width: int, height: int, orientation: int
) -> Tuple[bytes, int, int]:
    """把 BGR 帧按 EXIF Orientation 施加到像素，返回 (新字节, 新宽, 新高)。

    orientation 非 1..8 视为 1（原样）。1/2/3/4 保持宽高，5-8 交换宽高。
    """
    if len(data) != width * height * CHANNELS:
        raise ValueError("BGR 缓冲区长度与 width*height*3 不符")
    if orientation not in (2, 3, 4, 5, 6, 7, 8):
        return data, width, height

    if orientation in (2, 3, 4):
        new_w, new_h = width, height
    else:
        new_w, new_h = height, width
    out = bytearray(new_w * new_h * CHANNELS)

    def src_index(x: int, y: int) -> int:
        return (y * width + x) * CHANNELS

    for y in range(new_h):
        for x in range(new_w):
            if orientation == 2:  # 水平镜像
                sx, sy = width - 1 - x, y
            elif orientation == 3:  # 180°
                sx, sy = width - 1 - x, height - 1 - y
            elif orientation == 4:  # 垂直镜像
                sx, sy = x, height - 1 - y
            elif orientation == 5:  # 主对角转置
                sx, sy = y, x
            elif orientation == 6:  # 顺时针 90°
                sx, sy = y, height - 1 - x
            elif orientation == 7:  # 副对角转置
                sx, sy = width - 1 - y, height - 1 - x
            else:  # orientation == 8 逆时针 90°
                sx, sy = width - 1 - y, x
            si = src_index(sx, sy)
            di = (y * new_w + x) * CHANNELS
            out[di] = data[si]
            out[di + 1] = data[si + 1]
            out[di + 2] = data[si + 2]
    return bytes(out), new_w, new_h
