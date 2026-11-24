# -*- coding: utf-8 -*-
"""TASK-013 统一结果与 NMS 单测（``backend.vision.types`` / ``backend.vision.nms``）。

覆盖 design §8/§16（TEST-004「未命中空数组」「候选不拼成一次复合命中」「found≡matches 非空」）：
Candidate/TextBlock/RecognitionResult 校验、EvidenceLeaf 短路语义、NMS（IoU>0.5 保高）、
100 上限 + ``truncated``。纯函数，受管 Python 真实执行。
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.tests._vision_fakes import make_candidate, make_text_block, sample_match  # noqa: E402
from backend.types.capabilities import TypeValidationError  # noqa: E402
from backend.types.region import Point  # noqa: E402
from backend.vision.nms import MAX_CANDIDATES, cap_candidates, iou, nms  # noqa: E402
from backend.vision.types import (  # noqa: E402
    build_recognition_result,
    validate_candidate,
    validate_evidence_leaf,
    validate_recognition_result,
    validate_text_block,
)


class CandidateValidationTest(unittest.TestCase):
    def test_valid_candidate(self):
        candidate = validate_candidate(
            {"bbox": {"x": 0, "y": 0, "width": 4, "height": 4},
             "center": {"x": 2, "y": 2}, "confidence": 0.9, "space": "image_pixel"}
        )
        self.assertEqual(candidate.space, "image_pixel")
        self.assertEqual(candidate.center, Point(2, 2))

    def test_bad_confidence_rejected(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_candidate(
                {"bbox": {"x": 0, "y": 0, "width": 4, "height": 4},
                 "center": {"x": 2, "y": 2}, "confidence": 1.5, "space": "image_pixel"}
            )
        self.assertEqual(ctx.exception.code, "INVALID_PARAMS")
        self.assertEqual(ctx.exception.field, "candidate.confidence")

    def test_bad_space_rejected(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_candidate(
                {"bbox": {"x": 0, "y": 0, "width": 4, "height": 4},
                 "center": {"x": 2, "y": 2}, "confidence": 0.5, "space": "nope"}
            )
        self.assertEqual(ctx.exception.field, "candidate.space")


class TextBlockValidationTest(unittest.TestCase):
    def test_valid_text_block(self):
        block = validate_text_block({
            "text": "开始", "confidence": 0.95,
            "polygon": [{"x": 0, "y": 0}, {"x": 4, "y": 0}, {"x": 4, "y": 2}, {"x": 0, "y": 2}],
            "space": "image_pixel", "model_id": "m1", "language": "zh",
        })
        self.assertEqual(block.text, "开始")
        self.assertEqual(len(block.polygon), 4)

    def test_language_null_allowed(self):
        block = validate_text_block({
            "text": "x", "confidence": 0.5,
            "polygon": [{"x": 0, "y": 0}], "space": "image_pixel",
            "model_id": "m1", "language": None,
        })
        self.assertIsNone(block.language)

    def test_polygon_not_array_rejected(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_text_block({
                "text": "x", "confidence": 0.5, "polygon": "bad",
                "space": "image_pixel", "model_id": "m1", "language": None,
            })
        self.assertEqual(ctx.exception.field, "text_block.polygon")


class EvidenceShortCircuitTest(unittest.TestCase):
    def test_skipped_leaf_allows_null_matched(self):
        leaf = validate_evidence_leaf(
            {"condition_id": "c2", "matched": None, "confidence": None, "skipped": True}
        )
        self.assertTrue(leaf.skipped)
        self.assertIsNone(leaf.matched)

    def test_skipped_leaf_must_not_fabricate_score(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_evidence_leaf({"condition_id": "c2", "matched": True, "skipped": True})
        self.assertEqual(ctx.exception.field, "evidence.matched")


class RecognitionResultTest(unittest.TestCase):
    def test_build_forces_found_equiv_matches(self):
        result = build_recognition_result(
            target_id="t1", sample_no=1, target_revision=1, frame_id="f1",
            matches=(), elapsed_ms=1, truncated=False,
        )
        self.assertFalse(result.found)  # 未命中：found=False

    def test_validate_found_mismatch_rejected(self):
        with self.assertRaises(TypeValidationError) as ctx:
            validate_recognition_result({
                "target_id": "t1", "sample_no": 1, "target_revision": 1, "frame_id": "f1",
                "found": True, "matches": [], "elapsed_ms": 1, "truncated": False,
            })
        self.assertEqual(ctx.exception.field, "recognition_result.found")

    def test_validate_valid_result_roundtrip(self):
        match = sample_match()
        result = validate_recognition_result({
            "target_id": "t1", "sample_no": 1, "target_revision": 1, "frame_id": "f1",
            "found": True, "matches": [match.to_dict()], "elapsed_ms": 5, "truncated": False,
        })
        self.assertTrue(result.found)
        self.assertEqual(result.matches[0].target_id, "t1")


class NmsTest(unittest.TestCase):
    def test_iou_identity_and_disjoint(self):
        from backend.types.region import Rect
        a = Rect(x=0, y=0, width=4, height=4)
        self.assertAlmostEqual(iou(a, a), 1.0)
        b = Rect(x=100, y=100, width=4, height=4)
        self.assertEqual(iou(a, b), 0.0)

    def test_nms_suppresses_lower_confidence(self):
        high = make_candidate(0, 0, 4, 4, 0.9)
        low = make_candidate(0, 0, 4, 4, 0.8)  # IoU=1 > 0.5 → 被抑制
        kept = nms([low, high])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].confidence, 0.9)

    def test_nms_keeps_disjoint(self):
        a = make_candidate(0, 0, 4, 4, 0.9)
        b = make_candidate(50, 50, 4, 4, 0.8)
        self.assertEqual(len(nms([a, b])), 2)

    def test_cap_candidates_truncated(self):
        candidates = [make_candidate(i, 0, 1, 1, 0.5) for i in range(MAX_CANDIDATES + 10)]
        kept, truncated = cap_candidates(candidates)
        self.assertTrue(truncated)
        self.assertEqual(len(kept), MAX_CANDIDATES)

    def test_cap_candidates_not_truncated(self):
        kept, truncated = cap_candidates([make_candidate(0, 0, 1, 1, 0.5)])
        self.assertFalse(truncated)
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
