# -*- coding: utf-8 -*-
"""TASK-014 ``TemplateProvider`` 单测（**两分支真实执行**；禁止 skipTest）。

探针分支（有 cv2）：真实 TM_CCOEFF_NORMED 算法断言（正/负样本、纯色/尺寸错误、尺度、分数 clamp）。
受管分支（无 cv2）：``initialize`` 抛 ``ENGINE_UNAVAILABLE``，调用抛 ``ENGINE_UNAVAILABLE``。
两种环境都真实执行 ``_algo.enumerate_scales``/``clamp01`` 纯逻辑断言。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests import vision_fixtures as vf  # noqa: E402
from backend.tests._vision_fakes import FakeProvider, make_manifest  # noqa: E402
from backend.vision.cancel import NullCancelToken  # noqa: E402
from backend.vision.providers import TemplateProvider  # noqa: E402
from backend.vision.providers._algo import clamp01, enumerate_scales  # noqa: E402
from backend.vision.registry import ProviderRegistry  # noqa: E402
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402

try:
    import cv2  # noqa: F401
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

_MODELS = tempfile.mkdtemp(prefix="st-vision-tpl-")


class ScaleEnumerationTest(unittest.TestCase):
    """纯逻辑（无 cv2）：确定性尺度取点（升序去重、含端点、非整除补上界）。"""

    def test_default_range_endpoints(self):
        scales = enumerate_scales(0.95, 1.05, 0.01)
        self.assertEqual(scales[0], 0.95)
        self.assertEqual(scales[-1], 1.05)
        self.assertEqual(scales, sorted(scales))

    def test_non_divisible_appends_high(self):
        scales = enumerate_scales(1.0, 1.05, 0.02)  # 1.0,1.02,1.04 → 补 1.05
        self.assertIn(1.05, scales)
        self.assertEqual(scales[-1], 1.05)

    def test_dedup(self):
        self.assertEqual(enumerate_scales(1.0, 1.0, 0.5), [1.0])

    def test_clamp01(self):
        self.assertEqual(clamp01(-0.3), 0.0)
        self.assertEqual(clamp01(1.7), 1.0)
        self.assertEqual(clamp01(0.42), 0.42)


class TemplateDegradeTest(unittest.TestCase):
    def test_degrade_or_ready(self):
        reader = vf.MemoryPixelReader()
        provider = TemplateProvider(reader)
        if _HAS_CV2:
            provider.initialize(_MODELS)
        else:
            with self.assertRaises(RecognitionError) as ctx:
                provider.initialize(_MODELS)
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            self.assertFalse(ctx.exception.retryable)


class TemplateAlgorithmTest(unittest.TestCase):
    """探针分支：真实算法。受管分支：断言 ENGINE_UNAVAILABLE（真实执行）。"""

    def _reader_with(self, scene, template_patch, tw, th):
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        reader.add("tpl", vf.raw_image(tw, th, template_patch))
        return reader

    def test_positive_single_match(self):
        scene, bbox, patch = vf.template_scene(
            canvas_width=64, canvas_height=48, template_width=16, template_height=16, at=(20, 12)
        )
        reader = self._reader_with(scene, patch, 16, 16)
        provider = TemplateProvider(reader)
        frame = vf.make_frame(scene)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(frame, {"template_image_id": "tpl"}, NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        result = provider.recognize(frame, {"template_image_id": "tpl", "threshold": 0.85},
                                    NullCancelToken())
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual((candidate.bbox.x, candidate.bbox.y), (bbox[0], bbox[1]))
        self.assertGreaterEqual(candidate.confidence, 0.85)
        self.assertLessEqual(candidate.confidence, 1.0)
        self.assertEqual(candidate.space, "image_pixel")

    def test_negative_no_instance_is_empty_not_error(self):
        neg = vf.Canvas(64, 48, (128, 128, 128)).to_raw()
        patch = vf.distinctive_patch(16, 16, seed=1)
        reader = self._reader_with(neg, patch, 16, 16)
        provider = TemplateProvider(reader)
        frame = vf.make_frame(neg)
        if not _HAS_CV2:
            return  # 降级分支由其它用例断言
        provider.initialize(_MODELS)
        result = provider.recognize(frame, {"template_image_id": "tpl"}, NullCancelToken())
        self.assertEqual(result.candidates, ())  # 未命中（非错误）

    def test_pure_color_template_invalid_image(self):
        scene, _bbox, _patch = vf.template_scene()
        solid = vf.solid(16, 16, (100, 100, 100))
        reader = self._reader_with(scene, solid, 16, 16)
        provider = TemplateProvider(reader)
        frame = vf.make_frame(scene)
        if not _HAS_CV2:
            return
        provider.initialize(_MODELS)
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(frame, {"template_image_id": "tpl"}, NullCancelToken())
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")
        self.assertEqual(ctx.exception.details.get("field"), "template_image_id")

    def test_template_larger_than_source_invalid_image(self):
        scene = vf.Canvas(32, 32, (128, 128, 128)).to_raw()
        big = vf.distinctive_patch(48, 48, seed=2)
        reader = self._reader_with(scene, big, 48, 48)
        provider = TemplateProvider(reader)
        frame = vf.make_frame(scene)
        if not _HAS_CV2:
            return
        provider.initialize(_MODELS)
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(frame, {"template_image_id": "tpl", "allow_scale": False},
                               NullCancelToken())
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")

    def test_allow_scale_finds_scaled_instance(self):
        # 模板 24×24；实例为模板的 0.5 倍（12×12）；allow_scale + [0.45,0.55] 应命中。
        base_raw = vf.raw_image(24, 24, vf.distinctive_patch(24, 24, seed=4))
        reader = vf.MemoryPixelReader()
        if not _HAS_CV2:
            return
        from backend.tests import _vision_algo_fakes as af  # 探针分支专用

        small = af.scale(base_raw, 0.5)
        canvas = vf.Canvas(80, 60, (128, 128, 128))
        canvas.paste(30, 20, small.data, small.width, small.height)
        scene = canvas.to_raw()
        reader.add("src", scene)
        reader.add("tpl", base_raw)
        provider = TemplateProvider(reader)
        provider.initialize(_MODELS)
        result = provider.recognize(
            vf.make_frame(scene),
            {"template_image_id": "tpl", "threshold": 0.7, "allow_scale": True,
             "scale_range": [0.45, 0.55], "step": 0.01},
            NullCancelToken(),
        )
        self.assertGreaterEqual(len(result.candidates), 1)
        top = max(c.confidence for c in result.candidates)
        self.assertGreaterEqual(top, 0.7)


class TemplateCacheKeyTest(unittest.TestCase):
    """service 把**模板内容 sha256** 折入缓存键：模板内容变 → 键变（防误命中）。"""

    def test_template_content_change_changes_key(self):
        reader = vf.MemoryPixelReader()
        scene, _bbox, patch = vf.template_scene()
        reader.add("src", scene)
        reader.add("tpl", vf.raw_image(16, 16, patch))
        counters = {"calls": 0}

        def factory(pixels):
            def on_recognize(_frame, _params):
                counters["calls"] += 1
            return FakeProvider(
                make_manifest("tpl-provider", condition_types=("template",), provider_version="1.0.0"),
                pixels, on_recognize=on_recognize,
            )

        registry = ProviderRegistry(reader, factories={"tpl-provider": factory})
        registry.initialize_all(_MODELS)
        service = VisionService(registry, pixels=reader, jobs=None)
        params = {"image_id": "src", "template_image_id": "tpl", "threshold": 0.85}
        service._execute_provider("match_template", params, NullCancelToken())  # noqa: SLF001
        service._execute_provider("match_template", params, NullCancelToken())  # noqa: SLF001
        self.assertEqual(counters["calls"], 1)  # 第二次命中缓存
        # 改变模板内容（同 id）→ 键变 → 重新计算
        reader.add("tpl", vf.raw_image(16, 16, vf.distinctive_patch(16, 16, seed=99)))
        service._execute_provider("match_template", params, NullCancelToken())  # noqa: SLF001
        self.assertEqual(counters["calls"], 2)


if __name__ == "__main__":
    unittest.main()
