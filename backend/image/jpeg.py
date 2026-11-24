# -*- coding: utf-8 -*-
"""JPEG 解码（TASK-009）：标准库无 JPEG 解码器，经可选依赖 Pillow 实现。

- 延迟 import PIL：未安装 Pillow 时抛 ``JpegBackendUnavailableError``（import_service
  映射 ENGINE_UNAVAILABLE，**不伪称 jpeg 可用**）；
- 解码失败（坏字节）抛 ``JpegDecodeError``（映射 INVALID_IMAGE）；
- 输出 RGB uint8 行主序缓冲（import_service 转 BGR 后登记）。

依赖登记见 ``reports/TASK-009/依赖决策.md`` 与 ``manifest/dependencies.json``。
"""

from __future__ import annotations

import io
from typing import Tuple


class JpegDecodeError(ValueError):
    """JPEG 解码失败（坏字节/损坏）。"""


class JpegBackendUnavailableError(JpegDecodeError):
    """JPEG 解码后端（Pillow）不可用。"""


def pillow_available() -> bool:
    """Pillow 是否可导入（能力披露/测试用）。"""
    try:
        import PIL.Image  # noqa: F401
    except Exception:
        return False
    return True


def decode_jpeg_to_rgb(data: bytes) -> Tuple[int, int, bytes]:
    """解码 JPEG → (width, height, RGB 行主序 uint8)。

    Raises:
        JpegBackendUnavailableError: 未安装 Pillow。
        JpegDecodeError: 解码失败。
    """
    try:
        from PIL import Image  # 延迟 import：未安装时在此抛出
    except Exception as exc:  # pragma: no cover - 依赖环境
        raise JpegBackendUnavailableError("JPEG 解码需要 Pillow（未安装）") from exc

    try:
        with Image.open(io.BytesIO(data)) as image:
            rgb_image = image.convert("RGB")
            width, height = rgb_image.size
            rgb = rgb_image.tobytes()
    except Exception as exc:
        raise JpegDecodeError("JPEG 解码失败: %s" % exc) from exc
    if width <= 0 or height <= 0:
        raise JpegDecodeError("JPEG 尺寸必须为正")
    return width, height, rgb
