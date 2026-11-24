# -*- coding: utf-8 -*-
"""TASK-014 ``ColorProvider`` 单测（**两分支真实执行**；禁止 skipTest）。

探针分支：真实 8 连通分量 + RGB 逐通道绝对差 + HSV 圆周最短距离算法断言
（左右分离两团 → 2 个紧致候选；RGB 15/16 边界；hue 环回；覆盖量）。
受管分支：``initialize``/调用抛 ``ENGINE_UNAVAILABLE``；参数校验错误同步定位。

真值：两团方块的位置/尺寸取自生成器真值（``color_scene_two_blobs`` 返回），非实现输出。
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
from backend.vision.providers import ColorProvider  # noqa: E402
from backend.vision.providers._algo import (  # noqa: E402
    connected_components_8,
    hue_distance,
    hsv_within,
    rgb_within,
)
from backend.vision.types import RecognitionError  # noqa: E402

try:
    import cv2  # noqa: F401
    _HAS_CV2 = True
except Exception:
    _HAS_CV2 = False

_MODELS = tempfile.mkdtemp(prefix="st-vision-color-")

#: 两团夹具的方块颜色（BGR；R=200、G=30、B=30）。
_BLOB_BGR = (30, 30, 200)
_BLOB_RGB = {"r": 200, "g": 30, "b": 30}


class ColorDegradeTest(unittest.TestCase):
    def test_degrade_or_ready(self):
        provider = ColorProvider(vf.MemoryPixelReader())
        if _HAS_CV2:
            provider.initialize(_MODELS)
        else:
            with self.assertRaises(RecognitionError) as ctx:
                provider.initialize(_MODELS)
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")


class ColorParamValidationTest(unittest.TestCase):
    def _scene(self):
        scene = vf.Canvas(20, 20, (0, 0, 0)).to_raw()
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        return reader, scene

    def test_hue_360_invalid_params(self):
        reader, scene = self._scene()
        provider = ColorProvider(reader)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "hsv", "color": {"h": 0, "s": 50, "v": 50}}},
                    NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(vf.make_frame(scene), {"params": {
                "color_space": "hsv", "color": {"h": 360, "s": 50, "v": 50}}},
                NullCancelToken())
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "conditions.params.color.h")

    def test_hsc_color_space_invalid_params(self):
        reader, scene = self._scene()
        provider = ColorProvider(reader)
        if not _HAS_CV2:
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "hsc", "color": {"r": 1, "g": 2, "b": 3}}},
                    NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        provider.initialize(_MODELS)
        with self.assertRaises(RecognitionError) as ctx:
            provider.recognize(vf.make_frame(scene), {"params": {
                "color_space": "hsc", "color": {"r": 1, "g": 2, "b": 3}}},
                NullCancelToken())
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "conditions.params.color_space")


class ColorAlgorithmTest(unittest.TestCase):
    """探针 venv 真实算法断言（受管 Python 下断言 ENGINE_UNAVAILABLE）。"""

    def _recognize(self, reader, scene, color_params):
        provider = ColorProvider(reader)
        provider.initialize(_MODELS)
        return provider.recognize(vf.make_frame(scene), {"params": color_params},
                                  NullCancelToken())

    def test_two_blobs_two_tight_boxes(self):
        # 07 §2 硬要求：分散同色像素 → **每团紧致 bbox**，绝不拼成一个巨大框。
        scene, (left_box, right_box) = vf.color_scene_two_blobs(
            _BLOB_BGR, background=(0, 0, 0), blob_size=8, gap=16, margin=4)
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        if not _HAS_CV2:
            provider = ColorProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "rgb", "color": _BLOB_RGB}}, NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        result = self._recognize(reader, scene, {
            "color_space": "rgb", "color": _BLOB_RGB,
            "tolerance": {"r": 15, "g": 15, "b": 15}})
        self.assertEqual(len(result.candidates), 2)  # 两团 → 两个候选（非一个巨大框）
        boxes = sorted((c.bbox.x, c.bbox.y, c.bbox.width, c.bbox.height)
                       for c in result.candidates)
        self.assertEqual(boxes, sorted([left_box, right_box]))  # 各紧致 bbox == 真值
        for candidate in result.candidates:
            self.assertLessEqual(candidate.bbox.width, 8)
            self.assertLessEqual(candidate.bbox.height, 8)
            self.assertAlmostEqual(candidate.confidence, 1.0, places=6)  # 64/(8*8)
        self.assertEqual(result.candidates[0].space, "image_pixel")

    def test_rgb_tolerance_boundary(self):
        # 逐通道绝对差：±15 命中、±16 不命中。
        canvas = vf.Canvas(40, 16, (0, 0, 0))
        canvas.fill_rect(4, 4, 8, 8, (115, 115, 115))   # 各通道 +15 → 命中
        canvas.fill_rect(22, 4, 8, 8, (116, 116, 116))  # 各通道 +16 → 不命中
        scene = canvas.to_raw()
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        if not _HAS_CV2:
            provider = ColorProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "rgb", "color": {"r": 100, "g": 100, "b": 100}}},
                    NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        result = self._recognize(reader, scene, {
            "color_space": "rgb", "color": {"r": 100, "g": 100, "b": 100},
            "tolerance": {"r": 15, "g": 15, "b": 15}})
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual((result.candidates[0].bbox.x, result.candidates[0].bbox.y), (4, 4))

    def test_rgb_single_channel_over_tolerance_misses(self):
        # 单通道越界即不命中（逐通道 AND）。
        canvas = vf.Canvas(40, 16, (0, 0, 0))
        canvas.fill_rect(4, 4, 8, 8, (115, 115, 116))  # b 通道 +16 → 整像素不命中
        scene = canvas.to_raw()
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        if not _HAS_CV2:
            provider = ColorProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "rgb", "color": {"r": 100, "g": 100, "b": 100}}},
                    NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        result = self._recognize(reader, scene, {
            "color_space": "rgb", "color": {"r": 100, "g": 100, "b": 100},
            "tolerance": {"r": 15, "g": 15, "b": 15}})
        self.assertEqual(result.candidates, ())

    def test_hsv_hue_circular_wraparound(self):
        # hue 圆周最短距离：h_t=0、tol_h=10 ⇒ h_contract=358（距离 2）命中（线性距离 358 会漏）。
        canvas = vf.Canvas(40, 16, (0, 0, 0))
        canvas.fill_rect(4, 4, 8, 8, (5, 0, 255))    # BGR→ contract h=358（=360-2）
        canvas.fill_rect(22, 4, 8, 8, (0, 0, 255))   # 纯红 → contract h=0
        scene = canvas.to_raw()
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        if not _HAS_CV2:
            provider = ColorProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "hsv", "color": {"h": 0, "s": 100, "v": 100}}},
                    NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        # tol_h=10：h=358（距离 2）与 h=0（距离 0）都命中 → 2 个候选
        wide = self._recognize(reader, scene, {
            "color_space": "hsv", "color": {"h": 0, "s": 100, "v": 100},
            "tolerance": {"h": 10, "s": 20, "v": 20}})
        self.assertEqual(len(wide.candidates), 2)
        # tol_h=0：仅 h=0 命中（h=358 距离 2 > 0）→ 1 个候选，位于右块
        tight = self._recognize(reader, scene, {
            "color_space": "hsv", "color": {"h": 0, "s": 100, "v": 100},
            "tolerance": {"h": 0, "s": 20, "v": 20}})
        self.assertEqual(len(tight.candidates), 1)
        self.assertEqual((tight.candidates[0].bbox.x, tight.candidates[0].bbox.y), (22, 4))

    def test_min_percentage_whole_frame(self):
        # 分母=整帧：每团 8×8=64，帧 40×16=640 ⇒ 单团占 10%。
        scene, _ = vf.color_scene_two_blobs(_BLOB_BGR, blob_size=8, gap=16, margin=4)
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        if not _HAS_CV2:
            provider = ColorProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "rgb", "color": _BLOB_RGB}}, NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        base = {"color_space": "rgb", "color": _BLOB_RGB,
                "tolerance": {"r": 15, "g": 15, "b": 15}}
        below = dict(base, min_percentage=15.0)   # 10% < 15% → 全滤除
        self.assertEqual(self._recognize(reader, scene, below).candidates, ())
        above = dict(base, min_percentage=5.0)    # 10% ≥ 5% → 保留两团
        self.assertEqual(len(self._recognize(reader, scene, above).candidates), 2)

    def test_min_pixel_count(self):
        scene, _ = vf.color_scene_two_blobs(_BLOB_BGR, blob_size=8, gap=16, margin=4)
        reader = vf.MemoryPixelReader()
        reader.add("src", scene)
        if not _HAS_CV2:
            provider = ColorProvider(reader)
            with self.assertRaises(RecognitionError) as ctx:
                provider.recognize(vf.make_frame(scene), {"params": {
                    "color_space": "rgb", "color": _BLOB_RGB}}, NullCancelToken())
            self.assertEqual(ctx.exception.code, "ENGINE_UNAVAILABLE")
            return
        base = {"color_space": "rgb", "color": _BLOB_RGB,
                "tolerance": {"r": 15, "g": 15, "b": 15}}
        # 每团面积 64：min_pixel_count=65 全滤除；=64 全保留
        self.assertEqual(
            self._recognize(reader, scene, dict(base, min_pixel_count=65)).candidates, ())
        self.assertEqual(
            len(self._recognize(reader, scene, dict(base, min_pixel_count=64)).candidates), 2)


class ColorPureHelperTest(unittest.TestCase):
    """``_algo`` 纯逻辑辅助（无 cv2 路径）：**两环境都真实执行**（受管 Python 亦可断言）。"""

    def test_hue_circular_distance(self):
        self.assertEqual(hue_distance(0, 358), 2)
        self.assertEqual(hue_distance(358, 0), 2)
        self.assertEqual(hue_distance(1, 359), 2)
        self.assertEqual(hue_distance(0, 349), 11)
        self.assertEqual(hue_distance(0, 180), 180)
        self.assertEqual(hue_distance(0, 181), 179)

    def test_rgb_within_boundary(self):
        target = {"r": 100, "g": 100, "b": 100}
        tolerance = {"r": 15, "g": 15, "b": 15}
        self.assertTrue(rgb_within(115, 115, 115, target, tolerance))   # +15 命中
        self.assertFalse(rgb_within(116, 115, 115, target, tolerance))  # 单通道 +16 不命中
        self.assertTrue(rgb_within(85, 85, 85, target, tolerance))      # -15 命中
        self.assertFalse(rgb_within(84, 85, 85, target, tolerance))     # 单通道 -16 不命中

    def test_hsv_within_wraparound_and_float(self):
        target = {"h": 0, "s": 100, "v": 100}
        tolerance = {"h": 10, "s": 20, "v": 20}
        self.assertTrue(hsv_within(358, 100, 100, target, tolerance))  # 圆周距离 2
        self.assertFalse(hsv_within(349, 100, 100, target, tolerance))  # 圆周距离 11
        # s/v 浮点比较（契约域 0..100，不二次取整）
        self.assertTrue(hsv_within(0, 100.0, 100.0, target, tolerance))
        self.assertFalse(hsv_within(0, 100.0, 121.0, target, tolerance))

    def test_connected_components_8_tight_and_separate(self):
        # 两个左右分离的实心块 → 2 个分量，各自紧致 bbox（不拼大框）。
        rows = [[False] * 30 for _ in range(10)]
        for y in range(2, 6):
            for x in range(2, 6):
                rows[y][x] = True
        for y in range(2, 6):
            for x in range(22, 26):
                rows[y][x] = True
        comps = sorted(connected_components_8(rows))
        self.assertEqual(len(comps), 2)
        self.assertEqual(comps[0], (2, 2, 5, 5, 16))   # (min_x,min_y,max_x,max_y,area)
        self.assertEqual(comps[1], (22, 2, 25, 5, 16))

    def test_connected_components_8_diagonal_is_single(self):
        # 对角相邻（8 连通）→ 1 个分量（4 连通则会拆成 2）。
        rows = [[False] * 5 for _ in range(5)]
        rows[1][1] = True
        rows[2][2] = True
        comps = connected_components_8(rows)
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0], (1, 1, 2, 2, 2))


if __name__ == "__main__":
    unittest.main()
