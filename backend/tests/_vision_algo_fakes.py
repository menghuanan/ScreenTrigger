# -*- coding: utf-8 -*-
"""TASK-014 探针分支辅助（**仅探针 venv 导入**；受管 Python 下**不得**导入本模块）。

本模块在顶层 import ``cv2``/``numpy``——因此**只能**在 ``_ALGO_AVAILABLE`` 为真的
分支中导入；受管 Python（无 cv2/numpy）导入会抛 ImportError，测试用条件导入规避。

提供探针侧常用变换：加噪、旋转、缩放、灰度、BGR↔RGB（用于鲁棒性/形变夹具）。
"""

from __future__ import annotations

from typing import Any

import cv2  # noqa: F401
import numpy as np  # noqa: F401


def available() -> bool:
    """本模块可用（能导入即真）。"""
    return True


def to_array(raw: Any) -> Any:
    """RawImage/RawPixels（BGR 行主序）→ ``HxWx3`` uint8 ndarray。"""
    data = raw.bgr if hasattr(raw, "bgr") else raw.data
    return np.frombuffer(data, dtype=np.uint8).reshape(raw.height, raw.width, 3).copy()


def add_noise(raw: Any, sigma: float = 8.0, seed: int = 0) -> Any:
    """在 BGR 帧上叠加高斯噪声，返回新的 RawImage 等价（导入方自行包装）。"""
    rng = np.random.default_rng(seed)
    arr = to_array(raw).astype(np.int16)
    noise = rng.normal(0.0, sigma, arr.shape)
    noisy = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return _rebuild(raw, noisy)


def rotate(raw: Any, angle: float) -> Any:
    """绕中心旋转（同比例，不裁剪尺寸）。"""
    arr = to_array(raw)
    height, width = arr.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(arr, matrix, (width, height))
    return _rebuild(raw, rotated)


def scale(raw: Any, factor: float) -> Any:
    """等比缩放。"""
    arr = to_array(raw)
    height, width = arr.shape[:2]
    scaled = cv2.resize(
        arr, (max(1, int(width * factor)), max(1, int(height * factor))),
        interpolation=cv2.INTER_AREA,
    )
    return _rebuild(raw, scaled)


def to_gray(raw: Any) -> Any:
    """BGR→灰度 ndarray。"""
    return cv2.cvtColor(to_array(raw), cv2.COLOR_BGR2GRAY)


def _rebuild(raw: Any, array: Any) -> Any:
    from backend.capture.ports import RawImage

    height, width = array.shape[:2]
    return RawImage(width=int(width), height=int(height), data=array.tobytes())
