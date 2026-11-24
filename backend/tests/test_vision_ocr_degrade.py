# -*- coding: utf-8 -*-
"""TASK-015 T03 单测：``OCREngineProvider`` 诚实降级分支（**不 skip**，两环境真实执行）。

覆盖缺引擎/缺模型/损坏/引擎构造失败/预热失败/未初始化各分支的错误码与
``capabilities[ocr].reason`` 规范格式；「缺引擎」分支通过替换 ``_module_available`` 探针
在受管 Python 与探针 venv **一致**触发（不依赖环境是否安装 paddleocr）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_fakes import RecordingPixelReader  # noqa: E402
from backend.tests._vision_ocr_fakes import (  # noqa: E402
    FakePaddleOCR,
    FakePaddleOCRModule,
    build_fake_catalog,
    make_frame,
)
from backend.vision.cancel import NullCancelToken  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402
from backend.vision.providers import ocr as ocr_module  # noqa: E402
from backend.vision.providers.ocr import OCREngineProvider  # noqa: E402
from backend.vision.providers.ocr_models import parse_ocr_reason  # noqa: E402


class _FileNotFoundOCR:
    def __init__(self, **_kwargs):
        raise FileNotFoundError("local model dir missing")


class _BoomOCR:
    def __init__(self, **_kwargs):
        raise RuntimeError("mkldnn crash")


def _provider(*, catalog, library_root, paddleocr_module=None, seed_models_dir=None):
    provider = OCREngineProvider(
        RecordingPixelReader(), paddleocr_module=paddleocr_module, catalog=catalog,
        library_root=library_root,
    )
    return provider


def _assert_reason(testcase, exc):
    parsed = parse_ocr_reason(exc.message)
    testcase.assertIsNotNone(parsed, "reason 应为 OCR 规范格式: %r" % exc.message)
    testcase.assertEqual(parsed["state"], "unavailable")
    testcase.assertEqual(parsed["code"], exc.code)
    return parsed


class MissingEngineTest(unittest.TestCase):
    def test_missing_engine_raises_engine_unavailable(self):
        catalog, root = build_fake_catalog()
        provider = _provider(catalog=catalog, library_root=root)
        with mock.patch.object(ocr_module, "_module_available", return_value=False):
            with self.assertRaises(RecognitionError) as ctx:
                provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg-"))
        self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
        self.assertFalse(ctx.exception.retryable)
        _assert_reason(self, ctx.exception)

    def test_import_failure_raises_engine_unavailable(self):
        catalog, root = build_fake_catalog()
        provider = _provider(catalog=catalog, library_root=root)
        with mock.patch.object(ocr_module, "_module_available", return_value=True), \
             mock.patch.object(ocr_module.importlib, "import_module",
                               side_effect=ImportError("no paddlepaddle")):
            with self.assertRaises(RecognitionError) as ctx:
                provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg2-"))
        self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")


class MissingModelTest(unittest.TestCase):
    def test_missing_model_file_raises_model_unavailable(self):
        catalog, root = build_fake_catalog()
        # 库根下无模型文件 → verify 失败
        empty = tempfile.mkdtemp(prefix="st-ocr-empty-")
        provider = _provider(catalog=catalog, library_root=empty,
                             paddleocr_module=FakePaddleOCRModule())
        with self.assertRaises(RecognitionError) as ctx:
            provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg3-"))
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")
        _assert_reason(self, ctx.exception)

    def test_corrupt_model_raises_data_corrupt(self):
        catalog, root = build_fake_catalog(corrupt=True)
        provider = _provider(catalog=catalog, library_root=root,
                             paddleocr_module=FakePaddleOCRModule())
        with self.assertRaises(RecognitionError) as ctx:
            provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg4-"))
        self.assertEqual(ctx.exception.code, "DATA_CORRUPT")
        _assert_reason(self, ctx.exception)


class EngineConstructionTest(unittest.TestCase):
    def test_missing_files_at_construction_raises_model_unavailable(self):
        catalog, root = build_fake_catalog()
        module = FakePaddleOCRModule(_FileNotFoundOCR)
        provider = _provider(catalog=catalog, library_root=root, paddleocr_module=module)
        with self.assertRaises(RecognitionError) as ctx:
            provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg5-"))
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")

    def test_construction_error_raises_recognition_failed(self):
        catalog, root = build_fake_catalog()
        module = FakePaddleOCRModule(_BoomOCR)
        provider = _provider(catalog=catalog, library_root=root, paddleocr_module=module)
        with self.assertRaises(RecognitionError) as ctx:
            provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg6-"))
        self.assertEqual(ctx.exception.code, "RECOGNITION_FAILED")

    def test_warmup_error_raises_recognition_failed(self):
        FakePaddleOCR.reset()
        FakePaddleOCR.scripted = {"fake_rec": {"error": RuntimeError("warmup boom")}}
        catalog, root = build_fake_catalog()
        provider = _provider(catalog=catalog, library_root=root,
                             paddleocr_module=FakePaddleOCRModule())
        with self.assertRaises(RecognitionError) as ctx:
            provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg7-"))
        self.assertEqual(ctx.exception.code, "RECOGNITION_FAILED")


class CatalogLoadFailureTest(unittest.TestCase):
    def test_missing_catalog_file_raises_model_unavailable(self):
        provider = OCREngineProvider(RecordingPixelReader(), catalog=None)
        with mock.patch.object(ocr_module, "REPO_CATALOG_PATH",
                               os.path.join(tempfile.mkdtemp(), "absent.json")):
            with self.assertRaises(RecognitionError) as ctx:
                provider.initialize(tempfile.mkdtemp(prefix="st-ocr-deg8-"))
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")
        _assert_reason(self, ctx.exception)


class NotInitializedTest(unittest.TestCase):
    def test_recognize_before_initialize_raises_engine_unavailable(self):
        catalog, root = build_fake_catalog()
        provider = _provider(catalog=catalog, library_root=root)
        reader = provider._pixels  # noqa: SLF001
        reader.add("img-x")
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(make_frame(image_id="img-x"), {"model_id": "fake_rec"},
                               NullCancelToken())
        self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
