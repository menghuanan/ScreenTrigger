# -*- coding: utf-8 -*-
"""TASK-016 T16-4：条件树求值**契约快照**（``schema/contract-cases.json`` 同源）。

不依赖假 evaluator（对比 ``test_vision_contract``）：本用例用**真实**
``ConditionTreeEvaluator`` + 替身 provider 跑通，断言落点与公开契约逐字一致：

- 识别命令集合不变（6 条，对外总数仍 62）；
- 条件树两命令 job ``result`` 键集 = 用例 ``resultKeys``（``RecognitionResult`` 8 字段）；
- ``found ≡ matches 非空``（02：由 ``build_recognition_result`` 强制）；
- ``test_recognition`` 未保存草稿 → ``target_id``/``sample_no``/``target_revision`` = null；
- ``Match``/``EvidenceLeaf`` 的 wire 键集（02：策略层不新增同义字段）；
- 叶 ``roi`` 为 ``LocalRect``（宽高 ≥1，**非** Region，不受 10×10 限制）；
- 叶类型枚举四类 + 分支仅 all/any（无 NOT）；
- 叶数 1–10、嵌套 ≤4 由权威 ``_validate_condition_tree`` 保证（复用，不重造）。

运行：python -m unittest backend.tests.test_vision_condition_contract -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.ipc.jobs import JobManager  # noqa: E402
from backend.tests._vision_condition_fakes import (  # noqa: E402
    make_frame,
    make_registry,
    provider_returning,
)
from backend.tests._vision_fakes import (  # noqa: E402
    FakeSession,
    RecordingPixelReader,
    RecordingTransport,
)
from backend.types.capabilities import TypeValidationError  # noqa: E402
from backend.types.region import Point  # noqa: E402
from backend.vision.commands import register_vision_commands  # noqa: E402
from backend.vision.condition_evaluator import ConditionTreeEvaluator  # noqa: E402
from backend.vision.condition_serializer import (  # noqa: E402
    LEAF_TYPES,
    serialize_condition_tree,
)
from backend.vision.providers.base import make_candidate, make_text_block  # noqa: E402

_CASES_PATH = os.path.join(_REPO_ROOT, "schema", "contract-cases.json")

#: 02 叶类型枚举（四类，逐字）。
_EXPECTED_LEAF_TYPES = ("template", "contour", "ocr", "color")

#: ``RecognitionResult`` wire 键集（02 §8.1）。
_RESULT_KEYS = {
    "target_id", "sample_no", "target_revision", "frame_id",
    "found", "matches", "elapsed_ms", "truncated",
}

#: ``Match`` wire 键集（02：策略层不得新增同义字段）。
_MATCH_KEYS = {
    "target_id", "sample_no", "space", "bbox", "center",
    "confidence", "geometry_revision", "evidence",
}

#: ``EvidenceLeaf`` wire 键集（02 §8.3）。
_EVIDENCE_KEYS = {"condition_id", "matched", "confidence", "text", "skipped"}

_COLOR = {"color_space": "rgb", "color": {"r": 1, "g": 2, "b": 3}}


class _NullCancel:
    def throw_if_cancelled(self):
        return None


class _StubService:
    def submit(self, command: str, params: Dict[str, Any]) -> str:
        return "job-stub"


def _load_recognition_cases() -> List[Dict[str, Any]]:
    with open(_CASES_PATH, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    return [
        case for case in document["cases"]
        if case.get("kind") == "recognition_job" and "python" in case.get("applies_to", [])
    ]


def _registered_commands() -> set:
    transport = RecordingTransport()
    dispatcher = Dispatcher(
        transport=transport, session=FakeSession(), stderr=io.StringIO(),
        job_manager=JobManager(),
    )
    register_vision_commands(dispatcher, _StubService())
    return set(dispatcher._handlers.keys())


def _leaf(condition_id: str, leaf_type: str, params: Dict[str, Any],
          roi: Any = None) -> Dict[str, Any]:
    node: Dict[str, Any] = {"id": condition_id, "type": leaf_type, "params": params}
    if roi is not None:
        node["roi"] = roi
    return node


def _draft(conditions: Any, anchor: str, image_id: str = "tpl-1") -> Dict[str, Any]:
    return {
        "name": "契约草稿", "image_id": image_id,
        "anchor_condition_id": anchor, "conditions": conditions,
    }


def _text_block(text: str, confidence: float = 0.95):
    return make_text_block(
        text, confidence, [Point(0, 0), Point(1, 0), Point(1, 1), Point(0, 1)]
    )


def _evaluator(providers: Dict[str, Any]) -> ConditionTreeEvaluator:
    registry = make_registry(providers, RecordingPixelReader())
    return ConditionTreeEvaluator(registry=registry)


class CommandSetContractTest(unittest.TestCase):
    """命令集合契约：6 条识别命令不变，对外总数仍 62。"""

    def setUp(self) -> None:
        self.cases = _load_recognition_cases()

    def test_command_set_unchanged(self):
        case = next(c for c in self.cases if c["id"] == "recognition-job-command-set")
        self.assertEqual(set(case["commands"]), _registered_commands())
        self.assertEqual(len(case["commands"]), 6)


class EvaluatorResultContractTest(unittest.TestCase):
    """条件树两命令 job result 键集 = 用例 resultKeys（真实 evaluator 产出）。"""

    def setUp(self) -> None:
        self.cases = _load_recognition_cases()

    def _result_dict(self, *, force_draft: bool) -> Dict[str, Any]:
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        tree = _leaf("a1", "color", _COLOR)
        if force_draft:
            snapshot: Any = _draft(tree, "a1")
        else:
            from backend.tests._vision_condition_fakes import make_target_snapshot
            snapshot = make_target_snapshot(target_id="t-1", sample_no=1, revision=1,
                                            anchor="a1", conditions=tree)
        result = _evaluator({"builtin.color": color}).evaluate(
            make_frame(), snapshot, _NullCancel()
        )
        return result.to_dict()

    def test_evaluator_command_result_keys(self):
        for case in self.cases:
            command = case.get("command")
            if command not in ("evaluate_target_conditions", "test_recognition"):
                continue
            with self.subTest(case=case["id"]):
                force_draft = command == "test_recognition"
                payload = self._result_dict(force_draft=force_draft)
                self.assertEqual(set(payload.keys()), set(case["resultKeys"]))
                self.assertEqual(set(payload.keys()), _RESULT_KEYS)
                if case.get("draftNullIds"):
                    self.assertIsNone(payload["target_id"])
                    self.assertIsNone(payload["sample_no"])
                    self.assertIsNone(payload["target_revision"])

    def test_found_equiv_matches_nonempty(self):
        for force_draft in (True, False):
            with self.subTest(force_draft=force_draft):
                payload = self._result_dict(force_draft=force_draft)
                self.assertEqual(payload["found"], bool(payload["matches"]))
                self.assertTrue(payload["found"])


class MatchEvidenceShapeContractTest(unittest.TestCase):
    """``Match``/``EvidenceLeaf`` wire 键集（02；不新增同义字段）。"""

    def _match(self) -> Dict[str, Any]:
        color = provider_returning("color", candidates=(make_candidate(0, 0, 5, 5, 0.9),))
        ocr = provider_returning("ocr", text_blocks=(_text_block("OK"),))
        tree = {"op": "all", "children": [
            _leaf("a1", "color", _COLOR),
            _leaf("b1", "ocr", {"text": "OK", "model_id": "m1"}),
        ]}
        result = _evaluator({"builtin.color": color, "builtin.ocr": ocr}).evaluate(
            make_frame(), _draft(tree, "a1"), _NullCancel()
        )
        self.assertTrue(result.found)
        return result.matches[0].to_dict()

    def test_match_keys(self):
        self.assertEqual(set(self._match().keys()), _MATCH_KEYS)

    def test_evidence_keys(self):
        match = self._match()
        self.assertGreaterEqual(len(match["evidence"]), 2)
        for evidence in match["evidence"]:
            self.assertEqual(set(evidence.keys()), _EVIDENCE_KEYS)

    def test_anchor_evidence_never_skipped(self):
        match = self._match()
        anchor = next(e for e in match["evidence"] if e["condition_id"] == "a1")
        self.assertTrue(anchor["matched"])
        self.assertFalse(anchor["skipped"])


class ConditionTreeSchemaContractTest(unittest.TestCase):
    """条件树 schema 契约：叶类型四枚举、分支仅 all/any、roi=LocalRect。"""

    def test_leaf_type_enum(self):
        self.assertEqual(tuple(LEAF_TYPES), _EXPECTED_LEAF_TYPES)

    def test_branch_op_enum_rejects_not(self):
        tree = {"op": "not", "children": [_leaf("a1", "color", _COLOR)]}
        with self.assertRaises(TypeValidationError) as ctx:
            serialize_condition_tree(tree, "a1")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_leaf_roi_is_local_rect_not_region(self):
        # LocalRect 宽高 ≥1 即可（不受 Region 10×10 下限约束）。
        tree = _leaf("a1", "color", _COLOR, roi={"x": 0, "y": 0, "width": 1, "height": 1})
        spec = serialize_condition_tree(tree, "a1")
        self.assertIsNotNone(spec.anchor.roi)
        self.assertEqual((spec.anchor.roi.width, spec.anchor.roi.height), (1, 1))

    def test_leaf_roi_rejects_zero_size(self):
        tree = _leaf("a1", "color", _COLOR, roi={"x": 0, "y": 0, "width": 0, "height": 3})
        with self.assertRaises(TypeValidationError) as ctx:
            serialize_condition_tree(tree, "a1")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_leaf_count_bounds_reused(self):
        # 11 叶 → 超上限（权威 1–10 复用，不重造）。
        children = [_leaf("c%d" % i, "color", _COLOR) for i in range(11)]
        tree = {"op": "all", "children": children}
        with self.assertRaises(TypeValidationError) as ctx:
            serialize_condition_tree(tree, "c0")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_depth_upper_bound_reused(self):
        leaf_node = _leaf("c5", "color", _COLOR)
        b4 = {"op": "all", "children": [leaf_node]}
        b3 = {"op": "all", "children": [b4]}
        b2 = {"op": "all", "children": [b3]}
        b1 = {"op": "all", "children": [b2]}
        b0 = {"op": "all", "children": [b1]}
        # 5 层分支（b0..b4）→ 超 4 层上限（02：嵌套最多 4 层）
        with self.assertRaises(TypeValidationError) as ctx:
            serialize_condition_tree(b0, "c5")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")

    def test_depth_at_upper_bound_accepted(self):
        leaf_node = _leaf("c4", "color", _COLOR)
        b3 = {"op": "all", "children": [leaf_node]}
        b2 = {"op": "all", "children": [b3]}
        b1 = {"op": "all", "children": [b2]}
        b0 = {"op": "all", "children": [b1]}
        # 恰好 4 层分支 → 合法
        spec = serialize_condition_tree(b0, "c4")
        self.assertEqual(spec.anchor.condition_id, "c4")

    def test_anchor_must_exist(self):
        tree = _leaf("a1", "color", _COLOR)
        with self.assertRaises(TypeValidationError) as ctx:
            serialize_condition_tree(tree, "missing")
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.field, "anchor_condition_id")


if __name__ == "__main__":
    unittest.main()
