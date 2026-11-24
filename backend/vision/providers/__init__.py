# -*- coding: utf-8 -*-
"""``backend.vision.providers`` barrel（TASK-013 基座 + TASK-014 三算法 + TASK-015 OCR）。

- 基座组装类：``BaseProvider``（+ 替身 ``FakeProvider``/``NoopProvider``）；
- 算法 Provider（TASK-014）：``TemplateProvider``/``ContourProvider``/``ColorProvider``，
  公共基座 ``BaseAlgorithmProvider``；
- OCR Provider（TASK-015）：``OCREngineProvider``（第 4 个内置 Provider，``condition_type=ocr``）
  与模型目录 ``OcrModelCatalog``/``format_ocr_reason`` 等（``ocr_models``）；
- 惰性引擎：算法/OCR Provider **模块顶层不 import cv2/numpy/PIL/paddleocr**（见 ``_algo``/``ocr``）。

隔离：``providers/**`` 禁止 import ``targets.repository``/``ipc``/``emitter``/Input/``capture``
（由 ``test_vision_isolation.py`` 的 AST 断言固化）；Provider **只**得只读 ``PixelReader``。
"""

from __future__ import annotations

from ._algo import BaseAlgorithmProvider
from .base import (
    BaseProvider,
    FakeProvider,
    NoopProvider,
    make_candidate,
    make_text_block,
)
from .color import ColorProvider
from .contour import ContourProvider
from .ocr import OCREngineProvider
from .ocr_models import (
    OcrEngine,
    OcrModelCatalog,
    OcrModelEntry,
    OcrModelFile,
    format_ocr_reason,
    parse_ocr_reason,
    resolve_library_root,
    resolve_models_dir,
    seed_repo_model_metadata,
)
from .template import TemplateProvider

__all__ = [
    "BaseProvider",
    "BaseAlgorithmProvider",
    "FakeProvider",
    "NoopProvider",
    "make_candidate",
    "make_text_block",
    "TemplateProvider",
    "ContourProvider",
    "ColorProvider",
    "OCREngineProvider",
    "OcrModelCatalog",
    "OcrModelEntry",
    "OcrModelFile",
    "OcrEngine",
    "format_ocr_reason",
    "parse_ocr_reason",
    "resolve_models_dir",
    "resolve_library_root",
    "seed_repo_model_metadata",
]
