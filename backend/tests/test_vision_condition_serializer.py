# -*- coding: utf-8 -*-
"""TASK-016 T16-1：条件树解析/校验/规范化单测（``condition_serializer``）。

覆盖：DFS 前序有序 spec / anchor 标识、叶 1–10、深度 ≤4、叶 ID 唯一、anchor 存在性、
无 NOT、叶 params + roi 复用权威校验、``leaf_roi_in_frame`` 夹取与全外判 ``None``。

运行：python -m unittest backend.tests.test_vision_condition_serializer -v
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.types.capabilities import TypeValidationError  # noqa: E402
from backend.types.region import LocalRect, Rect  # noqa: E402
from backend.vision.condition_serializer import (  # noqa: E402
    BranchSpec,
    DraftSpec,
    LeafSpec,
    leaf_roi_in_frame,
    serialize_condition_tree,
    validate_draft,
)

_TEMPLATE = {"threshold": 0.9}
_COLOR = {"color_space": "rgb", "color": {"r": 10, "g": 20, "b": 30}}
_OCR = {"text": "OK", "model_id": "m1"}


def _leaf(cid, leaf_type="template", params=None, roi=None):
    node = {"id": cid, "type": leaf_type, "params": dict(params or _TEMPLATE)}
    if roi is not None:
        node["roi"] = roi
    return node


class SerializeValidTest(unittest.TestCase):
    def test_single_leaf_spec(self):
        tree = _leaf("c1", "template")
        spec = serialize_condition_tree(tree, "c1")
        self.assertIsInstance(spec.root, LeafSpec)
        self.assertEqual(len(spec.leaves), 1)
        self.assertTrue(spec.anchor.is_anchor)
        self.assertEqual(spec.anchor_condition_id, "c1")

    def test_dfs_preorder_ordering(self):
        # all( a(template, anchor), any( b(color), c(ocr) ), d(color) )
        tree = {
            "op": "all",
            "children": [
                _leaf("a", "template"),
                {"op": "any", "children": [_leaf("b", "color", _COLOR), _leaf("c", "ocr", _OCR)]},
                _leaf("d", "color", _COLOR),
            ],
        }
        spec = serialize_condition_tree(tree, "a")
        self.assertEqual([leaf.condition_id for leaf in spec.leaves], ["a", "b", "c", "d"])
        # 前序 order 严格递增（分支节点亦占序号）
        orders = [leaf.order for leaf in spec.leaves]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(set(orders)), len(orders))
        self.assertIsInstance(spec.root, BranchSpec)
        self.assertEqual(spec.root.op, "all")
        self.assertEqual(spec.anchor.condition_id, "a")

    def test_anchor_marks_correct_leaf(self):
        tree = {"op": "all", "children": [_leaf("x", "color", _COLOR), _leaf("y", "template")]}
        spec = serialize_condition_tree(tree, "y")
        self.assertEqual(spec.anchor.condition_id, "y")
        self.assertTrue(next(lf for lf in spec.leaves if lf.condition_id == "y").is_anchor)
        self.assertFalse(next(lf for lf in spec.leaves if lf.condition_id == "x").is_anchor)

    def test_leaf_roi_parsed(self):
        tree = _leaf("c1", "color", _COLOR, roi={"x": 2, "y": 3, "width": 5, "height": 6})
        spec = serialize_condition_tree(tree, "c1")
        roi = spec.leaves[0].roi
        self.assertEqual((roi.x, roi.y, roi.width, roi.height), (2, 3, 5, 6))


class SerializeInvalidTest(unittest.TestCase):
    def _assert_invalid(self, tree, anchor, field=None):
        with self.assertRaises(TypeValidationError) as ctx:
            serialize_condition_tree(tree, anchor)
        if field is not None:
            self.assertEqual(ctx.exception.field, field, msg=str(ctx.exception.message))
        return ctx.exception

    def test_anchor_missing(self):
        tree = _leaf("c1")
        self._assert_invalid(tree, "c-nope", field="anchor_condition_id")

    def test_not_operator_rejected(self):
        tree = {"op": "not", "children": [_leaf("c1")]}
        self._assert_invalid(tree, "c1")

    def test_duplicate_leaf_id(self):
        tree = {"op": "all", "children": [_leaf("c1"), _leaf("c1")]}
        self._assert_invalid(tree, "c1")

    def test_too_many_leaves(self):
        children = [_leaf("c%d" % i) for i in range(11)]
        self._assert_invalid({"op": "all", "children": children}, "c0")

    def test_empty_children(self):
        self._assert_invalid({"op": "all", "children": []}, "c0")

    def test_depth_over_four(self):
        # 5 层分支（第 5 层拒绝）
        node = _leaf("c1")
        for _ in range(5):
            node = {"op": "all", "children": [node]}
        self._assert_invalid(node, "c1")

    def test_bad_leaf_params_reuse_validator(self):
        tree = _leaf("c1", "template", {"threshold": 2.0})  # 越界 → 权威校验拒绝
        exc = self._assert_invalid(tree, "c1")
        self.assertTrue(str(exc.field).startswith("conditions.params"), str(exc.field))

    def test_bad_roi_reuse_validator(self):
        tree = _leaf("c1", "color", _COLOR, roi={"x": 0, "y": 0, "width": 0, "height": 4})
        exc = self._assert_invalid(tree, "c1")
        self.assertEqual(exc.field, "conditions.roi.width")

    def test_empty_anchor_rejected(self):
        self._assert_invalid(_leaf("c1"), "")


class ValidateDraftTest(unittest.TestCase):
    """草稿（TargetDraft）形状校验：复用 types.conditions 权威校验器（占位手法）。"""

    def _draft(self, **overrides):
        draft = {
            "name": "草稿",
            "image_id": "tpl-1",
            "anchor_condition_id": "c1",
            "conditions": _leaf("c1", "color", _COLOR),
        }
        draft.update(overrides)
        return draft

    def test_valid_draft_normalized(self):
        spec = validate_draft(self._draft())
        self.assertIsInstance(spec, DraftSpec)
        self.assertEqual(spec.name, "草稿")
        self.assertEqual(spec.image_id, "tpl-1")
        self.assertEqual(spec.anchor_condition_id, "c1")

    def test_name_trimmed_like_authoritative(self):
        spec = validate_draft(self._draft(name="  名称  "))
        self.assertEqual(spec.name, "名称")

    def test_undeclared_key_rejected(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft(self._draft(metadata={}))
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.field, "metadata")

    def test_missing_required_key(self):
        draft = self._draft()
        del draft["image_id"]
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft(draft)
        self.assertEqual(ctx.exception.field, "image_id")

    def test_storage_mode_without_image_id(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft({
                "name": "x", "anchor_condition_id": "c1", "storage_mode": "original",
                "conditions": _leaf("c1", "color", _COLOR),
            })
        self.assertEqual(ctx.exception.field, "storage_mode")

    def test_bad_storage_mode(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft(self._draft(storage_mode="raw"))
        self.assertEqual(ctx.exception.field, "storage_mode")

    def test_bad_name_reuse_authoritative(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft(self._draft(name="   "))
        self.assertEqual(ctx.exception.field, "name")

    def test_bad_conditions_reuse_authoritative(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft(self._draft(conditions=_leaf("c1", "template", {"threshold": 2.0})))
        self.assertTrue(str(ctx.exception.field).startswith("conditions.params"),
                        str(ctx.exception.field))

    def test_anchor_must_exist(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_draft(self._draft(anchor_condition_id="nope"))
        self.assertEqual(ctx.exception.field, "anchor_condition_id")

    def test_duplicate_leaf_id_rejected(self):
        tree = {"op": "all", "children": [_leaf("c1", "color", _COLOR),
                                          _leaf("c1", "color", _COLOR)]}
        with self.assertRaises(TypeValidationError):
            validate_draft(self._draft(conditions=tree))


class LeafRoiInFrameTest(unittest.TestCase):
    def _leaf_spec(self, roi):
        return LeafSpec(condition_id="x", type="color", params={}, roi=roi, order=0,
                        is_anchor=False)

    def test_no_roi_uses_candidate_bbox(self):
        spec = self._leaf_spec(None)
        out = leaf_roi_in_frame(Rect(x=10, y=10, width=20, height=20), spec, 100, 100)
        self.assertEqual((out.x, out.y, out.width, out.height), (10, 10, 20, 20))

    def test_roi_relative_to_candidate(self):
        spec = self._leaf_spec(LocalRect(x=2, y=3, width=5, height=6))
        out = leaf_roi_in_frame(Rect(x=10, y=10, width=20, height=20), spec, 100, 100)
        self.assertEqual((out.x, out.y, out.width, out.height), (12, 13, 5, 6))

    def test_roi_clamped_to_frame(self):
        spec = self._leaf_spec(LocalRect(x=0, y=0, width=40, height=40))
        out = leaf_roi_in_frame(Rect(x=90, y=90, width=20, height=20), spec, 100, 100)
        self.assertEqual((out.x, out.y, out.width, out.height), (90, 90, 10, 10))

    def test_fully_outside_returns_none(self):
        spec = self._leaf_spec(LocalRect(x=50, y=50, width=5, height=5))
        out = leaf_roi_in_frame(Rect(x=90, y=90, width=20, height=20), spec, 100, 100)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
