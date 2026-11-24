# -*- coding: utf-8 -*-
"""TASK-015 T03 单测：``OCREngineProvider`` 契约（受管 Python，paddleocr 替身真实执行）。

以 `_vision_ocr_fakes` 的可编程 paddleocr 替身，真实驱动 Provider 的
**TextBlock 转换 / 坐标 / 语言 / 取消 / 缓存指纹 / 能力披露**；不依赖真 paddleocr：

- ``initialize``：显式本地模型名 + 目录（Q12），固定 ``enable_mkldnn=False``，seed 仓内元数据；
- ``recognize`` 输出：``text`` 原文、``confidence`` clamp、``polygon`` 源图 0 基像素、``space=frame.space``、
  ``model_id``、``language``（模型身份语言，无法判定 → ``None``）；
- **Q1**：Provider **不做** origin→frame.space 平移（polygon 为源图像素，消费层用
  ``translate_polygon_to_space`` 平移）；``space=="screen_physical"`` 仅来自帧元数据，不伪造；
- 未知 ``model_id`` → ``MODEL_UNAVAILABLE``（field=model_id）；未初始化 → ``ENGINE_UNAVAILABLE``；
- 协作式取消（首行取消点）；``cache_fingerprint`` 折入模型内容 hash + 语言；
- **F-01/F-02 回归**：``rec_texts``/``rec_scores``/``rec_polys``/``rec_boxes`` 可能是
  ``numpy.ndarray``（仅支持 ``len()``、``bool()`` 抛错），用鸭子类型替身 ``_ArrayLike``
  覆盖「禁用 ``or`` 真值判断」；转换期异常归为 ``RECOGNITION_FAILED``（非未命中）；
  真实 paddleocr 端到端 ``recognize_text``（探针 venv，受管 Python 走诚实降级分支）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_fakes import RecordingPixelReader  # noqa: E402
from backend.tests._vision_ocr_fakes import (  # noqa: E402
    FakeOCRResult,
    FakePaddleOCR,
    FakePaddleOCRModule,
    build_fake_catalog,
    make_frame,
)
from backend.vision.cancel import CancelFlag, NullCancelToken  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402
from backend.vision.providers.ocr import OCREngineProvider  # noqa: E402
from backend.vision.textmatch import translate_polygon_to_space  # noqa: E402


class _ArrayLike:
    """模拟 ``numpy.ndarray`` 语义：支持 ``len()``/下标/迭代，``bool()`` 抛 ``ValueError``。"""

    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, index):
        return self._rows[index]

    def __iter__(self):
        return iter(self._rows)

    def __bool__(self):
        raise ValueError("The truth value of an array with more than one element is ambiguous")


class _StaticReader:
    """只读像素端口替身：返回单张预置图（供真实引擎端到端测试）。"""

    def __init__(self) -> None:
        self._raw = None

    def set_image(self, raw) -> None:
        self._raw = raw

    def read(self, image_id: str):
        return self._raw


def _make_provider(
    *,
    scripted=None,
    languages=("zh", "en"),
    primary_language="zh",
    base=None,
    reader=None,
):
    """构造并 initialize 一个使用替身引擎的 Provider（返回 provider/reader/base/models_dir）。"""
    FakePaddleOCR.reset()
    if scripted is not None:
        FakePaddleOCR.scripted = scripted
    catalog, lib_root = build_fake_catalog(
        languages=languages, primary_language=primary_language, root=base
    )
    reader = reader if reader is not None else RecordingPixelReader()
    provider = OCREngineProvider(
        reader, paddleocr_module=FakePaddleOCRModule(), catalog=catalog, library_root=lib_root
    )
    models_dir = tempfile.mkdtemp(prefix="st-ocr-models-")
    provider.initialize(models_dir)
    return provider, reader, lib_root, models_dir


def _script(texts, scores=None, polys=None):
    return {"fake_rec": {"results": [{"texts": texts, "scores": scores, "polys": polys}]}}


class InitializeTest(unittest.TestCase):
    def test_builds_engine_with_explicit_local_models(self):
        provider, _reader, lib_root, _models = _make_provider()
        self.assertEqual(len(FakePaddleOCR.instances), 1)
        kwargs = FakePaddleOCR.instances[0].kwargs
        # Q12：显式传本地模型名 + 目录（仅 dir 会与默认名不匹配）
        self.assertEqual(kwargs["text_detection_model_name"], "fake_det")
        self.assertEqual(kwargs["text_recognition_model_name"], "fake_rec")
        self.assertTrue(kwargs["text_detection_model_dir"].endswith("fake_det"))
        self.assertTrue(kwargs["text_recognition_model_dir"].endswith("fake_rec"))
        # PIR/onednn 崩溃规避 + 关闭 doc 预处理流水线
        self.assertFalse(kwargs["enable_mkldnn"])
        self.assertFalse(kwargs["use_doc_orientation_classify"])
        self.assertFalse(kwargs["use_doc_unwarping"])
        self.assertFalse(kwargs["use_textline_orientation"])
        self.assertEqual(provider.library_root, lib_root)

    def test_seeds_repo_metadata_into_models_dir(self):
        _provider, _reader, _lib, models_dir = _make_provider()
        self.assertTrue(
            os.path.isfile(os.path.join(models_dir, "PP-OCRv6_medium_rec", "manifest.json"))
        )

    def test_disclosure_reports_sources(self):
        provider, _reader, lib_root, models_dir = _make_provider()
        disclosure = provider.disclosure()
        self.assertEqual(disclosure["provider_id"], "builtin.ocr")
        self.assertEqual(disclosure["models_dir"], models_dir)
        self.assertEqual(disclosure["library_root"], lib_root)
        self.assertEqual(disclosure["library_source"], "config")
        self.assertTrue(disclosure["initialized"])

    def test_capabilities_manifest(self):
        provider, *_ = _make_provider()
        manifest = provider.capabilities()
        self.assertEqual(manifest.provider_id, "builtin.ocr")
        self.assertEqual(manifest.supported_condition_types, ("ocr",))
        self.assertIn("zh", manifest.supported_languages)
        self.assertTrue(manifest.offline)


class RecognizeTest(unittest.TestCase):
    def test_text_block_conversion(self):
        provider, reader, _lib, _models = _make_provider(
            scripted=_script(["Hello"], [0.9], [[[0, 0], [8, 0], [8, 8], [0, 8]]])
        )
        reader.add("img-1")
        frame = make_frame(image_id="img-1", space="image_pixel")
        result = provider.recognize(frame, {"model_id": "fake_rec"}, NullCancelToken())
        self.assertEqual(len(result.text_blocks), 1)
        block = result.text_blocks[0]
        self.assertEqual(block.text, "Hello")
        self.assertAlmostEqual(block.confidence, 0.9)
        self.assertEqual(block.space, "image_pixel")
        self.assertEqual(block.model_id, "fake_rec")
        self.assertEqual(block.language, "zh")
        self.assertEqual([(p.x, p.y) for p in block.polygon],
                         [(0, 0), (8, 0), (8, 8), (0, 8)])

    def test_korean_language_from_model_identity(self):
        provider, reader, _lib, _models = _make_provider(
            scripted=_script(["안녕"], [0.8]), languages=("en", "zh", "ko"), primary_language="ko"
        )
        reader.add("img-2")
        frame = make_frame(image_id="img-2")
        result = provider.recognize(frame, {"model_id": "fake_rec"}, NullCancelToken())
        self.assertEqual(result.text_blocks[0].language, "ko")

    def test_generic_model_language_is_none(self):
        provider, reader, _lib, _models = _make_provider(
            scripted=_script(["A"], [0.5]), primary_language=None
        )
        reader.add("img-3")
        result = provider.recognize(make_frame(image_id="img-3"), {"model_id": "fake_rec"},
                                    NullCancelToken())
        self.assertIsNone(result.text_blocks[0].language)

    def test_unknown_model_id_raises_model_unavailable(self):
        provider, reader, _lib, _models = _make_provider(scripted=_script(["A"], [0.5]))
        reader.add("img-4")
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(make_frame(image_id="img-4"), {"model_id": "nope"},
                               NullCancelToken())
        self.assertEqual(ctx.exception.code, "MODEL_UNAVAILABLE")
        self.assertEqual(ctx.exception.details.get("field"), "model_id")

    def test_deadline_cancel_before_inference(self):
        provider, reader, _lib, _models = _make_provider(scripted=_script(["A"], [0.5]))
        reader.add("img-5")
        flag = CancelFlag()
        flag.cancel()
        from backend.vision.cancel import CancelledError
        with self.assertRaises(CancelledError):
            provider.recognize(make_frame(image_id="img-5"), {"model_id": "fake_rec"}, flag)


class CoordinateIsolationTest(unittest.TestCase):
    """Q1：Provider 不做 origin 平移；`space` 恒等于 `frame.space`。"""

    def test_space_equals_frame_space_and_no_origin_translation(self):
        provider, reader, _lib, _models = _make_provider(
            scripted=_script(["T"], [0.9], [[[0, 0], [8, 0], [8, 8], [0, 8]]])
        )
        reader.add("img-6")
        frame = make_frame(image_id="img-6", space="screen_physical", origin=(5, 6))
        result = provider.recognize(frame, {"model_id": "fake_rec"}, NullCancelToken())
        block = result.text_blocks[0]
        self.assertEqual(block.space, "screen_physical")
        # polygon 保持源图 0 基像素（**未**被 origin 平移）
        self.assertEqual([(p.x, p.y) for p in block.polygon],
                         [(0, 0), (8, 0), (8, 8), (0, 8)])
        # 平移由消费层完成且只做一次
        moved = translate_polygon_to_space(block.polygon, frame.origin)
        self.assertEqual([(p.x, p.y) for p in moved],
                         [(5, 6), (13, 6), (13, 14), (5, 14)])


class TextBlockEdgeTest(unittest.TestCase):
    """`_to_text_blocks` 边界：boxes 回退、分数 clamp、缺几何跳过。"""

    def _provider(self):
        provider, _reader, _lib, _models = _make_provider()
        return provider

    def test_polygon_from_rec_boxes_when_polys_absent(self):
        provider = self._provider()
        result = [{"rec_texts": ["A"], "rec_scores": [0.5], "rec_boxes": [[10, 20, 30, 40]]}]
        blocks = provider._to_text_blocks(  # noqa: SLF001
            result, "fake_rec", make_frame(), NullCancelToken()
        )
        self.assertEqual([(p.x, p.y) for p in blocks[0].polygon],
                         [(10, 20), (30, 20), (30, 40), (10, 40)])

    def test_confidence_clamped(self):
        provider = self._provider()
        result = [{"rec_texts": ["hi", "lo"], "rec_scores": [2.0, -1.0],
                   "rec_polys": [[[0, 0], [1, 0], [1, 1], [0, 1]],
                                 [[2, 2], [3, 2], [3, 3], [2, 3]]]}]
        blocks = provider._to_text_blocks(  # noqa: SLF001
            result, "fake_rec", make_frame(), NullCancelToken()
        )
        self.assertEqual(blocks[0].confidence, 1.0)
        self.assertEqual(blocks[1].confidence, 0.0)

    def test_block_without_geometry_skipped(self):
        provider = self._provider()
        result = [{"rec_texts": ["A"], "rec_scores": [0.5]}]  # 无 polys / boxes
        blocks = provider._to_text_blocks(  # noqa: SLF001
            result, "fake_rec", make_frame(), NullCancelToken()
        )
        self.assertEqual(blocks, ())


class ArrayLikeResultTest(unittest.TestCase):
    """F-01/F-02 回归：paddleocr 字段可能是 ndarray（仅支持 ``len()``，``bool()`` 抛错）。

    真实 ``OCRResult.rec_boxes`` 为 ``numpy.ndarray``；受管 Python 无 numpy，用**鸭子类型**
    替身 ``_ArrayLike`` 复现同一语义，使该缺陷在**无 numpy** 环境也可回归覆盖。
    """

    def _provider(self):
        provider, _reader, _lib, _models = _make_provider()
        return provider

    def test_rec_boxes_arraylike_converts_without_crash(self):
        provider = self._provider()
        result = [{
            "rec_texts": _ArrayLike(["Start"]),
            "rec_scores": _ArrayLike([0.97]),
            "rec_polys": _ArrayLike([]),  # 空 → 回退 rec_boxes
            "rec_boxes": _ArrayLike([_ArrayLike([10, 20, 30, 40])]),
        }]
        blocks = provider._to_text_blocks(  # noqa: SLF001
            result, "fake_rec", make_frame(), NullCancelToken()
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "Start")
        self.assertEqual([(p.x, p.y) for p in blocks[0].polygon],
                         [(10, 20), (30, 20), (30, 40), (10, 40)])

    def test_rec_polys_arraylike_converts_without_crash(self):
        provider = self._provider()
        result = [{
            "rec_texts": _ArrayLike(["A"]),
            "rec_scores": _ArrayLike([0.5]),
            "rec_polys": _ArrayLike([_ArrayLike([_ArrayLike([0, 0]), _ArrayLike([8, 0]),
                                                 _ArrayLike([8, 8]), _ArrayLike([0, 8])])]),
        }]
        blocks = provider._to_text_blocks(  # noqa: SLF001
            result, "fake_rec", make_frame(), NullCancelToken()
        )
        self.assertEqual([(p.x, p.y) for p in blocks[0].polygon],
                         [(0, 0), (8, 0), (8, 8), (0, 8)])

    def test_as_sequence_explicit_none_only(self):
        provider = self._provider()
        sentinel = _ArrayLike([])
        self.assertEqual(provider._as_sequence(None), ())  # noqa: SLF001
        self.assertIs(provider._as_sequence(sentinel), sentinel)  # noqa: SLF001


class ConversionErrorClassificationTest(unittest.TestCase):
    """F-01 要求 2：转换期异常必须归为**故障**（RECOGNITION_FAILED），绝不静默变未命中。"""

    def test_conversion_error_is_recognition_failed(self):
        provider, reader, _lib, _models = _make_provider(scripted=_script(["A"], [0.5]))
        reader.add("img-conv")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("bad geometry")

        provider._to_text_blocks = _boom  # noqa: SLF001
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(make_frame(image_id="img-conv"), {"model_id": "fake_rec"},
                               NullCancelToken())
        self.assertEqual(ctx.exception.code, "RECOGNITION_FAILED")
        self.assertFalse(ctx.exception.retryable)


class RealEngineEndToEndTest(unittest.TestCase):
    """真实 paddleocr 端到端 ``recognize_text``（探针 venv）；受管 Python 走诚实降级分支。

    两分支**都真实执行**（非 skip）：探针 venv 真跑通并产出 blocks（覆盖 rec_boxes ndarray
    路径）；受管 Python 因缺引擎抛 ``ENGINE_UNAVAILABLE``（断言其错误码）。
    """

    def test_recognize_text_end_to_end(self):
        reader = _StaticReader()
        provider = OCREngineProvider(reader)
        try:
            provider.initialize(tempfile.mkdtemp(prefix="st-ocr-real-"))
        except RecognitionError as exc:
            self.assertIn(exc.code, ("ENGINE_UNAVAILABLE", "MODEL_UNAVAILABLE"))
            return
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        from backend.vision.ports import RawPixels
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 36)
        image = Image.new("RGB", (420, 100), "white")
        ImageDraw.Draw(image).text((20, 30), "Start", fill="black", font=font)
        bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
        height, width = bgr.shape[:2]
        reader.set_image(RawPixels(image_id="img-real", width=width, height=height,
                                   sha256="a" * 64, format="png", bgr=bgr.tobytes()))
        frame = make_frame(image_id="img-real", space="image_pixel", width=width, height=height)
        result = provider.recognize(frame, {"model_id": "PP-OCRv6_medium_rec"}, NullCancelToken())
        self.assertGreaterEqual(len(result.text_blocks), 1)
        self.assertTrue(any("start" in block.text.lower() for block in result.text_blocks))


class CacheFingerprintTest(unittest.TestCase):
    def test_fingerprint_contains_model_hash_and_language(self):
        provider, _reader, _lib, _models = _make_provider(
            languages=("en", "zh", "ko"), primary_language="ko"
        )
        fingerprint = provider.cache_fingerprint("fake_rec")
        self.assertEqual(len(fingerprint["model_sha256"]), 64)
        self.assertEqual(fingerprint["language"], "ko")

    def test_fingerprint_empty_for_unknown(self):
        provider, _reader, _lib, _models = _make_provider()
        self.assertEqual(provider.cache_fingerprint(None), {})
        self.assertEqual(provider.cache_fingerprint("unknown")["model_sha256"], "")


class CloseTest(unittest.TestCase):
    def test_close_is_idempotent(self):
        provider, _reader, _lib, _models = _make_provider()
        provider.close()
        provider.close()  # 幂等，不抛


if __name__ == "__main__":
    unittest.main()
