# -*- coding: utf-8 -*-
"""统一解码入口（TASK-009）：把 PNG/JPEG 字节解码为规范 BGR 派生图。

- PNG：标准库解码器（``png_decode``），零第三方依赖；
- JPEG：可选依赖 Pillow（``jpeg``），未安装抛 ENGINE_UNAVAILABLE（不伪称可用）；
- 解码后按 EXIF Orientation 应用方向（02：识别派生图应用方向）；
- 统一把底层解码异常映射为 06 错误码（INVALID_IMAGE / ENGINE_UNAVAILABLE）。
"""

from __future__ import annotations

from typing import Tuple

from ..capture.errors import CaptureError, invalid_image
from .exif import read_orientation
from .jpeg import JpegBackendUnavailableError, JpegDecodeError, decode_jpeg_to_rgb
from .pixelops import apply_orientation, rgb_to_bgr
from .png_decode import PngDecodeError, decode_png_to_rgb


def decode_image_to_bgr(data: bytes, image_format: str) -> Tuple[bytes, int, int]:
    """解码为 (BGR 字节, 宽, 高)；已应用 EXIF 方向（如有）。

    Raises:
        CaptureError: INVALID_IMAGE（坏图）/ ENGINE_UNAVAILABLE（JPEG 无 Pillow）。
    """
    if image_format == "png":
        try:
            width, height, rgb = decode_png_to_rgb(data)
        except PngDecodeError as exc:
            raise invalid_image("PNG 解码失败: %s" % exc, field="format") from exc
    elif image_format == "jpeg":
        try:
            width, height, rgb = decode_jpeg_to_rgb(data)
        except JpegBackendUnavailableError as exc:
            raise CaptureError(
                "ENGINE_UNAVAILABLE",
                "当前引擎未安装 JPEG 解码依赖（Pillow），无法导入 JPEG",
                retryable=False,
            ) from exc
        except JpegDecodeError as exc:
            raise invalid_image("JPEG 解码失败: %s" % exc, field="format") from exc
    else:
        raise invalid_image("未知图像格式: %r" % (image_format,), field="format")

    bgr = rgb_to_bgr(rgb, width, height)
    orientation = read_orientation(data, image_format)
    if orientation != 1:
        bgr, width, height = apply_orientation(bgr, width, height, orientation)
    return bgr, width, height
