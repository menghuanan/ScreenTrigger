# -*- coding: utf-8 -*-
"""TASK-013 识别 job 契约用例一致性测试（``schema/contract-cases.json`` 三端同源，kind=recognition_job）。

- 6 条识别命令全部 job 化注册（``register_vision_commands``），命令集合与用例逐字一致
  （**无命令集合变更，对外总数仍 62**）；
- 各命令 job ``result`` 键集与用例 ``resultKeys`` 逐字一致：``recognize_text``→``{blocks}``；
  ``match_*``/``detect_color``→``{matches, truncated}``（Q4/02）；条件树两命令 → ``RecognitionResult``；
- ``test_recognition`` 草稿结果 ``target_id``/``sample_no`` 为 null。

运行：python -m unittest backend.tests.test_vision_contract -v
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
from backend.tests._vision_fakes import (  # noqa: E402
    FakeConditionEvaluator,
    FakeSession,
    RecordingTransport,
    make_candidate,
    make_text_block,
)
from backend.types.region import Point  # noqa: E402
from backend.vision.commands import register_vision_commands  # noqa: E402
from backend.vision.service import VisionService  # noqa: E402
from backend.vision.types import ProviderResult  # noqa: E402

_CASES_PATH = os.path.join(_REPO_ROOT, "schema", "contract-cases.json")

# 命令 → 归一用的条件类型。
_CONDITION_OF_COMMAND = {
    "recognize_text": "ocr",
    "match_template": "template",
    "match_contour": "contour",
    "detect_color": "color",
}


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
        transport=transport, session=FakeSession(), stderr=io.StringIO(), job_manager=JobManager()
    )
    register_vision_commands(dispatcher, _StubService())
    return set(dispatcher._handlers.keys())


class RecognitionJobContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = _load_recognition_cases()

    def test_cases_exist(self):
        self.assertGreaterEqual(len(self.cases), 6)

    def test_command_set_matches_registration(self):
        case = next(c for c in self.cases if c["id"] == "recognition-job-command-set")
        self.assertEqual(set(case["commands"]), _registered_commands())

    def test_provider_command_result_keys(self):
        for case in self.cases:
            command = case.get("command")
            if command not in _CONDITION_OF_COMMAND:
                continue
            with self.subTest(case=case["id"]):
                condition_type = _CONDITION_OF_COMMAND[command]
                if condition_type == "ocr":
                    provider_result = ProviderResult(
                        text_blocks=(make_text_block("x", 0.9, [Point(0, 0)]),)
                    )
                else:
                    provider_result = ProviderResult(candidates=(make_candidate(0, 0, 4, 4, 0.9),))
                normalized = VisionService._normalize_provider_result(condition_type, provider_result)
                self.assertEqual(set(normalized.keys()), set(case["resultKeys"]))
                if "missIsError" in case:
                    miss = VisionService._normalize_provider_result(condition_type, ProviderResult())
                    self.assertEqual(miss["matches"], [])
                    self.assertFalse(miss["truncated"])

    def test_evaluator_command_result_keys(self):
        for case in self.cases:
            command = case.get("command")
            if command not in ("evaluate_target_conditions", "test_recognition"):
                continue
            with self.subTest(case=case["id"]):
                force_draft = command == "test_recognition"
                evaluator = FakeConditionEvaluator()
                result = _run_evaluator_normalize(evaluator, force_draft=force_draft)
                self.assertEqual(set(result.keys()), set(case["resultKeys"]))
                if case.get("draftNullIds"):
                    self.assertIsNone(result["target_id"])
                    self.assertIsNone(result["sample_no"])


def _run_evaluator_normalize(evaluator: FakeConditionEvaluator, *, force_draft: bool) -> Dict[str, Any]:
    frame = _FakeFrame()
    result = evaluator.evaluate(frame, object(), _NullCancel())
    return VisionService._normalize_result(result, frame.frame_id, force_draft=force_draft)


class _FakeFrame:
    frame_id = "frame-x"


class _NullCancel:
    def throw_if_cancelled(self):
        return None


if __name__ == "__main__":
    unittest.main()
