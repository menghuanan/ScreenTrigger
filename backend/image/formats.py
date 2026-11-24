# -*- coding: utf-8 -*-
"""图像格式识别与解码前预算（TASK-009，M01 import_image）。

零第三方依赖：只用 ``struct`` 读取文件头，**不解码像素**即可得到宽高，用于
「先检查解码预算（06：导入≤50MiB；解码≤40,000,000 像素，宽高各≤16384）」。

- ``extension_format``：按文件扩展名映射（``.png``/``.jpg``/``.jpeg``）；
- ``detect_format``：按魔数（内容）识别，用于**伪装扩展名**检查；
- ``header_dimensions``：读 PNG IHDR / JPEG SOFn 头得到 (width,height)。

类型/合法性判定错误一律抛 ``ValueError`` 子类（``ImageFormatError``），由
``import_service`` 映射为 06 错误码（INVALID_IMAGE / RESOURCE_LIMIT）。
"""

from __future__ import annotations

import struct
from typing import Optional, Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"

# 扩展名 → 内部格式（02：ImageRef.format 为 "png"|"jpeg"）
EXTENSION_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
}

# 内部格式 → 规范扩展名（资产落盘 images/<sha256>.<ext>）
FORMAT_EXTENSIONS = {
    "png": "png",
    "jpeg": "jpg",
}

# JPEG SOFn（Start Of Frame）标记：只取含帧尺寸的 SOF0-3/5-7/9-11（排除 DHT/DAC 等）
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class ImageFormatError(ValueError):
    """图像格式/头部非法（由 import_service 映射 INVALID_IMAGE）。"""


def extension_format(filename: str) -> Optional[str]:
    """按扩展名返回内部格式；未知扩展名返回 None。

    filename 可为完整路径或仅文件名；只取最后一段扩展名（大小写不敏感）。
    """
    if not isinstance(filename, str) or not filename:
        return None
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return None
    return EXTENSION_FORMATS.get(name[dot:].lower())


def detect_format(data: bytes) -> Optional[str]:
    """按魔数（内容）识别格式；未知返回 None。"""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8:
        return None
    if bytes(data[:8]) == PNG_SIGNATURE:
        return "png"
    if bytes(data[:3]) == JPEG_SIGNATURE:
        return "jpeg"
    return None


def png_dimensions(data: bytes) -> Tuple[int, int]:
    """从 PNG IHDR 读 (width,height)（不解码像素）。

    Raises:
        ImageFormatError: 签名/IHDR 非法。
    """
    if len(data) < 24 or bytes(data[:8]) != PNG_SIGNATURE:
        raise ImageFormatError("非 PNG 签名")
    if bytes(data[12:16]) != b"IHDR":
        raise ImageFormatError("PNG 缺少 IHDR")
    (width, height) = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise ImageFormatError("PNG IHDR 宽高必须为正")
    return width, height


def jpeg_dimensions(data: bytes) -> Tuple[int, int]:
    """扫描 JPEG 段头，找到 SOFn 得 (width,height)（不解码像素）。

    Raises:
        ImageFormatError: 非 JPEG 或无 SOF 帧头。
    """
    if len(data) < 4 or bytes(data[:2]) != b"\xff\xd8":
        raise ImageFormatError("非 JPEG 签名（缺少 SOI）")
    pos = 2
    total = len(data)
    while pos + 1 < total:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker == 0xFF:  # 填充字节
            pos -= 1
            continue
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue  # 无长度字段
        if pos + 1 >= total:
            break
        (seg_len,) = struct.unpack(">H", data[pos:pos + 2])
        if seg_len < 2:
            raise ImageFormatError("JPEG 段长度非法")
        if marker in _JPEG_SOF_MARKERS:
            if pos + 7 > total:
                raise ImageFormatError("JPEG SOF 段截断")
            (height, width) = struct.unpack(">HH", data[pos + 3:pos + 7])
            if width <= 0 or height <= 0:
                raise ImageFormatError("JPEG 尺寸必须为正")
            return width, height
        pos += seg_len
    raise ImageFormatError("未找到 JPEG SOF 帧头")


def header_dimensions(data: bytes, image_format: str) -> Tuple[int, int]:
    """按格式读文件头宽高（解码前预算用）。

    Raises:
        ImageFormatError: 未知格式或头部非法。
    """
    if image_format == "png":
        return png_dimensions(data)
    if image_format == "jpeg":
        return jpeg_dimensions(data)
    raise ImageFormatError("未知图像格式: %r" % (image_format,))
