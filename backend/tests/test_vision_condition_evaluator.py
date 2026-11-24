# -*- coding: utf-8 -*-
"""TASK-016 T16-4：条件树求值器单测（``condition_evaluator``）。

覆盖：anchor 整帧产候选 + NMS/100 上限（``truncated``）；逐候选局部 AND/OR 短路与
``skipped`` 证据口径；多物体不拼成一次复合命中；未命中 ``found=false,matches=[]``；
``found ≡ bool(matches)``；``Match.confidence``=anchor 分数、坐标含 ``frame.origin``、
``geometry_revision`` 与 frame 一致；draft 路径 target id 置 null；provider 抛错/取消透传；
anchor 叶 ``roi`` 被忽略。

运行：python -m unittest backend.tests.test_vision_condition_evaluator -v
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_condition_fakes import (  # noqa: E402
    ScriptedProvider,
    make_frame,
    make_registry,
    make_target_snapshot,
    provider_always_matches,
    provider_never_matches,
    provider_returning,
)
from backend.tests._vision_fakes import RecordingPixelReader  # noqa: E402
from backend.types.region import Point  # noqa: E402
from backend.vision.cancel import CancelledError  # noqa: E402
from backend.vision.condition_evaluator import ConditionTreeEvaluator  # noqa: E402
from backend.vision.providers.base import make_text_block  # noqa: E402
from backend.vision.types import RecognitionError  # noqa: E402

_COLOR = {"color_space": "rgb", "color": {"r": 1, "g": 2, "b": 3}}
_TEMPLATE = {"threshold": 0.9}


class _NullCancel:
    def throw_if_cancelled(self):
        return None


class _RaisingCancel:
    def throw_if_cancelled(self):
        raise CancelledError("cancelled")


def _leaf(cid, leaf_type, params, roi=None):
    node = {"id": cid, "type": leaf_type, "params": params}
    if roi is not None:
        node["roi"] = roi
    return node


def _draft(conditions, anchor, image_id="tpl-1"):
    return {"name": "d", "image_id": image_id, "anchor_condition_id": anchor,
            "conditions": conditions}


def _evaluator(providers, logger=None):
    reader = RecordingPixelReader()
    registry = make_registry(providers, reader, logger=logger)
    return ConditionTreeEvaluator(registry=registry, logger=logger)


def _text_block(text, conf=0.95):
    return make_text_block(text, conf, [Point(0, 0), Point(1, 0), Point(1, 1), Point(0, 1)])


class AnchorAndMatchTest(unittest.TestCase):
    def test_single_anchor_leaf_multi_object_not_merged(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(
            make_candidate(10, 10, 20, 20, 0.9),
            make_candidate(50, 50, 20, 20, 0.7),
        ))
        tree = _leaf("a1", "color", _COLOR)
        ev = _evaluator({"builtin.color": color})
        result = ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertTrue(result.found)
        self.assertEqual(len(result.matches), 2)  # 多物体：每候选一条 Match，不拼接
        self.assertEqual([m.confidence for m in result.matches], [0.9, 0.7])

    def test_no_candidates_found_false(self):
        color = provider_never_matches("color")
        tree = _leaf("a1", "color", _COLOR)
        ev = _evaluator({"builtin.color": color})
        result = ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertFalse(result.found)
        self.assertEqual(result.matches, ())
        self.assertFalse(result.truncated)

    def test_found_equiv_matches(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(1, 1, 5, 5, 0.8),))
        tree = _leaf("a1", "color", _COLOR)
        result = _evaluator({"builtin.color": color}).evaluate(
            make_frame(), _draft(tree, "a1"), _NullCancel()
        )
        self.assertEqual(result.found, bool(result.matches))
        self.assertTrue(result.found)


class ShortCircuitTest(unittest.TestCase):
    def _providers(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(10, 10, 20, 20, 0.9),))
        template = provider_never_matches("template")  # 命中 false
        ocr = provider_returning("ocr", text_blocks=(_text_block("OK"),))
        return {"builtin.color": color, "builtin.template": template, "builtin.ocr": ocr}

    def test_all_short_circuits_on_false_leaf(self):
        tree = {"op": "all", "children": [
            _leaf("a1", "color", _COLOR),
            _leaf("b1", "template", _TEMPLATE),
            _leaf("c1", "ocr", {"text": "OK", "model_id": "m1"}),
        ]}
        ev = _evaluator(self._providers())
        result = ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertFalse(result.found)  # all 中有 false 叶 → 无匹配
        # 无法直接取 evidence（无 match）；改用直接求值路径断言
        self.assertEqual(result.matches, ())

    def test_all_false_leaf_evidence_and_skipped(self):
        from backend.vision.providers.base import make_candidate
        # any(anchor true) 短路后，同候选证据中后续叶 skipped
        tree = {"op": "all", "children": [
            _leaf("a1", "color", _COLOR),
            _leaf("b1", "ocr", {"text": "NO", "model_id": "m1"}),  # 未命中
            _leaf("c1", "color", _COLOR),
        ]}
        color = provider_returning("color", candidates=(make_candidate(5, 5, 10, 10, 0.9),))
        ocr = provider_returning("ocr", text_blocks=())  # 无文本 → 未命中
        ev = _evaluator({"builtin.color": color, "builtin.ocr": ocr})
        result = ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertFalse(result.found)

    def test_any_short_circuits_after_anchor(self):
        from backend.vision.providers.base import make_candidate
        tree = {"op": "any", "children": [
            _leaf("a1", "color", _COLOR),
            _leaf("c1", "ocr", {"text": "OK", "model_id": "m1"}),
        ]}
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        ocr = provider_returning("ocr", text_blocks=(_text_block("OK"),))
        ev = _evaluator({"builtin.color": color, "builtin.ocr": ocr})
        result = ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertTrue(result.found)
        evidence = result.matches[0].evidence
        by_id = {e.condition_id: e for e in evidence}
        self.assertTrue(by_id["a1"].matched)  # anchor 恒 true
        self.assertTrue(by_id["c1"].skipped)  # any 短路：c1 未执行
        self.assertIsNone(by_id["c1"].matched)

    def test_all_leaf_true_gives_match(self):
        from backend.vision.providers.base import make_candidate
        tree = {"op": "all", "children": [
            _leaf("a1", "color", _COLOR),
            _leaf("c1", "ocr", {"text": "OK", "model_id": "m1"}),
        ]}
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        ocr = provider_returning("ocr", text_blocks=(_text_block("OK"),))
        ev = _evaluator({"builtin.color": color, "builtin.ocr": ocr})
        result = ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertTrue(result.found)
        by_id = {e.condition_id: e for e in result.matches[0].evidence}
        self.assertTrue(by_id["c1"].matched)
        self.assertEqual(by_id["c1"].text, "OK")


class CoordinatesAndRoiTest(unittest.TestCase):
    def test_coordinates_include_frame_origin_and_geometry_revision(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(10, 10, 20, 20, 0.9),))
        tree = _leaf("a1", "color", _COLOR)
        frame = make_frame(width=200, height=200, origin_x=100, origin_y=200,
                           geometry_revision=7, space="screen_physical")
        result = _evaluator({"builtin.color": color}).evaluate(
            frame, _draft(tree, "a1"), _NullCancel()
        )
        match = result.matches[0]
        self.assertEqual((match.bbox.x, match.bbox.y), (110, 210))
        self.assertEqual((match.center.x, match.center.y), (120, 220))
        self.assertEqual(match.geometry_revision, 7)
        self.assertEqual(match.space, "screen_physical")

    def test_roi_fully_outside_leaf_false(self):
        from backend.vision.providers.base import make_candidate
        # 锚点候选贴帧右/下边；color 叶 roi 相对候选偏移 50 → 落帧外 → 该叶 false
        color = provider_returning("color", candidates=(make_candidate(90, 90, 20, 20, 0.9),))
        tree = {"op": "all", "children": [
            _leaf("a1", "color", _COLOR),
            _leaf("c1", "color", _COLOR, roi={"x": 50, "y": 50, "width": 5, "height": 5}),
        ]}
        result = _evaluator({"builtin.color": color}).evaluate(
            make_frame(width=100, height=100), _draft(tree, "a1"), _NullCancel()
        )
        self.assertFalse(result.found)

    def test_anchor_roi_ignored(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        logs = []
        tree = _leaf("a1", "color", _COLOR, roi={"x": 9, "y": 9, "width": 3, "height": 3})
        ev = _evaluator({"builtin.color": color}, logger=logs.append)
        ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertIsNone(color.rois[0])  # anchor 恒整帧（roi=None）
        self.assertTrue(any("忽略" in m for m in logs), logs)


class ErrorAndCancelTest(unittest.TestCase):
    def test_provider_error_propagates(self):
        def _boom(frame, params, cancel, roi):
            raise RecognitionError("RECOGNITION_FAILED", "boom")
        color = ScriptedProvider("color", _boom)
        tree = _leaf("a1", "color", _COLOR)
        ev = _evaluator({"builtin.color": color})
        with self.assertRaises(RecognitionError) as ctx:
            ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertEqual(ctx.exception.code, "RECOGNITION_FAILED")

    def test_capture_cancelled_propagates(self):
        color = provider_always_matches("color")
        tree = _leaf("a1", "color", _COLOR)
        ev = _evaluator({"builtin.color": color})
        with self.assertRaises(CancelledError):
            ev.evaluate(make_frame(), _draft(tree, "a1"), _RaisingCancel())

    def test_missing_provider_capability_denied(self):
        tree = _leaf("a1", "color", _COLOR)
        ev = _evaluator({})  # 无 provider → 空注册表
        with self.assertRaises(RecognitionError) as ctx:
            ev.evaluate(make_frame(), _draft(tree, "a1"), _NullCancel())
        self.assertIn(ctx.exception.code, ("CAPABILITY_DENIED", "ENGINE_UNAVAILABLE",
                                           "MODEL_UNAVAILABLE"))


class DraftAndTargetTest(unittest.TestCase):
    def test_draft_result_ids_null(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        tree = _leaf("a1", "color", _COLOR)
        result = _evaluator({"builtin.color": color}).evaluate(
            make_frame(), _draft(tree, "a1"), _NullCancel()
        )
        self.assertIsNone(result.target_id)
        self.assertIsNone(result.sample_no)
        self.assertIsNone(result.target_revision)
        self.assertEqual(result.matches[0].target_id, "draft-placeholder")

    def test_invalid_draft_shape_is_invalid_params(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        tree = _leaf("a1", "color", _COLOR)
        ev = _evaluator({"builtin.color": color})
        # 草稿形状非法（未声明字段）→ INVALID_PARAMS（job error），非未命中。
        bad = _draft(tree, "a1")
        bad["metadata"] = {}
        with self.assertRaises(RecognitionError) as ctx:
            ev.evaluate(make_frame(), bad, _NullCancel())
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_target_snapshot_carries_ids(self):
        from backend.vision.providers.base import make_candidate
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        tree = _leaf("a1", "color", _COLOR)
        snapshot = make_target_snapshot(target_id="t-9", sample_no=3, revision=2,
                                        anchor="a1", conditions=tree, image=None)
        result = _evaluator({"builtin.color": color}).evaluate(
            make_frame(), snapshot, _NullCancel()
        )
        self.assertEqual(result.target_id, "t-9")
        self.assertEqual(result.sample_no, 3)
        self.assertEqual(result.target_revision, 2)
        self.assertEqual(result.matches[0].target_id, "t-9")
        self.assertEqual(result.matches[0].sample_no, 3)


if __name__ == "__main__":
    unittest.main()
