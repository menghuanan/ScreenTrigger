# -*- coding: utf-8 -*-
"""TASK-014 ``ContourProvider`` 单测（**两分支真实执行**；禁止 skipTest）。

探针分支：真实灰度/Canny/matchShapes I1 算法断言（形状命中、形变旋转不变、
**纹理变色但形状不变** → 轮廓命中且模板失配）。
受管分支：``initialize``/调用抛 ``ENGINE_UNAVAILABLE``；参数校验错误同步定位。

夹具设计说明（为何用「内侧描边 + 纹理」）：Canny 外轮廓要闭合，形状**边界**须为
**均匀强边缘**。若形状被纹理填满，纹理暗块触及边界会打断边缘 → 外轮廓碎片化。
``striped_patch`` 的 ``border_bgr`` 在形状**内侧**画实色描边：边界 → 强且均匀；
内部纹理的边被描边包围（成洞）→ ``RETR_EXTERNAL`` 只取到干净外轮廓。真值取自
生成器（形状/位置/朝向），非实现输出。
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
from backend.vision.cancel import NullCancelToken  # noqa: E402
from backend.vision.providers import ContourProvider, TemplateProvider  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402

try:
    import cv2  # noqa: F401
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

_MODELS = tempfile.mkdtemp(prefix="st-vision-contour-")


class ContourDegradeTest(unittest.TestCase):
    def test_degrade_or_ready(self):
        provider = ContourProvider(vf.MemoryPixelReader())
        if _HAS_CV2:
            provider.initialize(_MODELS)
        else:
            with self.assertRaises(RecognitionError) as ctx:
                provider.initialize(_MODELS)
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")


class ContourParamValidationTest(unittest.TestCase):
    def test_canny_low_ge_high_invalid_params(self):
        reader = vf.MemoryPixelReader()
        scene = vf.Canvas(40, 40, (0, 0, 0)).to_raw()
        reader.add("src", scene)
        reader.add("tpl", vf.raw_image(10, 10, vf.filled_patch(vf.rect_mask(10, 10), (200, 200, 200))))
        provider = ContourProvider(reader)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                                   NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(
                vf.make_frame(scene),
                {"template_image_id": "tpl", "canny_low": 150, "canny_high": 100},
                NullCancelToken(),
            )
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "conditions.params.canny_high")


class ContourAlgorithmTest(unittest.TestCase):
    """探针 venv 真实算法断言（受管 Python 下断言 ENGINE_UNAVAILABLE）。"""

    def test_shape_hit(self):
        mask = vf.circle_mask(24, 10)
        solid = vf.filled_patch(mask, (200, 200, 200))
        scene, bbox = vf.shape_scene(mask, solid, (20, 15), 60, 50, background=(0, 0, 0))
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        reader.add("tpl", vf.raw_image(24, 24, solid))
        provider = ContourProvider(reader)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                                   NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        result = provider.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                                    NullCancelToken())
        self.assertGreaterEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].space, "image_pixel")
        # 命中位置接近真值（Canny 边缘包围盒与形状真值相差数像素内）
        candidate = result.candidates[0]
        self.assertLessEqual(abs(candidate.bbox.x - bbox[0]), 4)
        self.assertLessEqual(abs(candidate.bbox.y - bbox[1]), 4)

    def test_texture_change_shape_invariance_beats_template(self):
        # 07 §2 硬要求：同一外轮廓、内部纹理/颜色不同 → 轮廓命中、模板失配。
        # 形状=圆（外轮廓不变）；参考=竖条纹、实例=横条纹（正交纹理 → 像素显著不同）。
        mask = vf.circle_mask(24, 10)
        reference = vf.striped_patch(mask, period=4, orient="v", low=60, high=255,
                                     border_bgr=255, border=2)
        instance = vf.striped_patch(mask, period=4, orient="h", low=60, high=255,
                                    border_bgr=255, border=2)
        scene, _bbox = vf.shape_scene(mask, instance, (20, 15), 60, 50, background=(0, 0, 0))
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        reader.add("tpl", vf.raw_image(24, 24, reference))
        if not _HAS_CV2:
            provider = ContourProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                                   NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        contour = ContourProvider(reader)
        contour.initialize(_MODELS)
        cres = contour.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                                 NullCancelToken())
        self.assertGreaterEqual(len(cres.candidates), 1)  # 形状不变 → 轮廓命中
        self.assertGreaterEqual(cres.candidates[0].confidence, 0.85)

        template = TemplateProvider(reader)
        template.initialize(_MODELS)
        tres = template.recognize(vf.make_frame(scene),
                                  {"template_image_id": "tpl", "threshold": 0.85},
                                  NullCancelToken())
        self.assertEqual(tres.candidates, ())  # 纹理不同 → 模板失配
        # 反证：模板对**自身**素材自匹配应命中（证明模板可用，非"永远失配"）。
        self_match = template.recognize(vf.make_frame(vf.raw_image(24, 24, reference), "tpl"),
                                        {"template_image_id": "tpl", "threshold": 0.85},
                                        NullCancelToken())
        self.assertGreaterEqual(len(self_match.candidates), 1)

    def test_rotation_invariance(self):
        # 形变：参考=轴对齐矩形；实例=同尺寸矩形旋转 35°（Hu 矩旋转不变 → 轮廓命中）。
        reference = vf.rotated_rect_mask(40, 14, 24, 0.0)
        rotated = vf.rotated_rect_mask(40, 14, 24, 35.0)
        ref_solid = vf.filled_patch(reference, (210, 210, 210))
        rot_solid = vf.filled_patch(rotated, (210, 210, 210))
        scene = vf.Canvas(60, 60, (0, 0, 0))
        scene.paste(10, 10, rot_solid, 40, 40)
        scene_raw = scene.to_raw()
        reader = vf.MemoryPixelReader()
        reader.add("src", scene_raw)
        reader.add("tpl", vf.raw_image(40, 40, ref_solid))
        provider = ContourProvider(reader)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene_raw), {"template_image_id": "tpl"},
                                   NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        result = provider.recognize(vf.make_frame(scene_raw),
                                    {"template_image_id": "tpl", "threshold": 0.8},
                                    NullCancelToken())
        self.assertGreaterEqual(len(result.candidates), 1)  # Hu 矩旋转不敏感

    def test_reference_without_contour_invalid_image(self):
        # 参考为纯色（无任何边缘）→ Canny 后无外轮廓 → INVALID_IMAGE（field=template_image_id）。
        scene = vf.Canvas(40, 40, (0, 0, 0)).to_raw()
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        reader.add("tpl", vf.raw_image(16, 16, vf.solid(16, 16, (200, 200, 200))))
        provider = ContourProvider(reader)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                                   NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(vf.make_frame(scene), {"template_image_id": "tpl"},
                               NullCancelToken())
        self.assertEqual(ctx.exception.code, "INVALID_IMAGE")
        self.assertEqual(ctx.exception.details.get("field"), "template_image_id")


if __name__ == "__main__":
    unittest.main()
