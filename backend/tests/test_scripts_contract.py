# -*- coding: utf-8 -*-
"""TASK-012 契约用例一致性测试（``schema/contract-cases.json`` 三端同源）。

与 ``test_types_contract.py``（kind=type_validation）及 Rust ``contract_tests.rs``、
TS ``contractCases.ts`` 消费**同一份** ``contract-cases.json``：

- ``kind="type_validation"`` 且 ``typeName="script"``（``applies_to`` 含 python）：
  纯结构校验 ``validate_type("script", value)`` 与 ``expect``（valid/code/field）逐字一致；
- 断言 TASK-012 新增的 3 条 script 结构用例（name-101 / parameters-非数组 / 正例）存在
  且 ``applies_to`` 含 rust（三端同源，非 Python 独享）。

脚本**领域**规则（源码 UTF-8/字节预算、参数唯一性、``default`` 类型）不在实体
``validate_script`` 内，其执行端为 ``test_scripts_domain``（故不作 type_validation 用例）。

运行：python -m unittest backend.tests.test_scripts_contract -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.types import TypeValidationError, validate_type  # noqa: E402

_CASES_PATH = os.path.join(_REPO_ROOT, "schema", "contract-cases.json")


def _load_cases() -> Dict[str, Any]:
    with open(_CASES_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _apply_overrides(case: Dict[str, Any], value: Any) -> Any:
    override_length = case.get("nameOverrideLength")
    if override_length is not None and isinstance(value, dict):
        value = dict(value)
        value["name"] = "a" * int(override_length)
    return value


class ScriptTypeValidationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = _load_cases()

    def _script_cases(self, applies_to: str) -> List[Dict[str, Any]]:
        return [
            case for case in self.document["cases"]
            if case.get("kind") == "type_validation"
            and case.get("typeName") == "script"
            and applies_to in case.get("applies_to", [])
        ]

    def test_script_cases_exist(self) -> None:
        self.assertGreaterEqual(
            len(self._script_cases("python")), 4, "TASK-012 应有 script 结构校验用例"
        )

    def test_script_cases_are_shared_with_rust(self) -> None:
        self.assertGreaterEqual(
            len(self._script_cases("rust")), 4, "script 结构用例必须 applies_to 含 rust"
        )

    def test_new_task012_cases_present(self) -> None:
        ids = {case["id"] for case in self._script_cases("python")}
        for expected in (
            "type-script-name-101-invalid",
            "type-script-parameters-not-array-invalid",
            "type-script-minimal-valid",
        ):
            self.assertIn(expected, ids)

    def test_script_type_validation_matches_expect(self) -> None:
        for case in self._script_cases("python"):
            with self.subTest(case=case["id"]):
                value = _apply_overrides(case, case["value"])
                expect = case["expect"]
                try:
                    validate_type(case["typeName"], value)
                    actual_valid = True
                    actual_code = None
                    actual_field = None
                except TypeValidationError as error:
                    actual_valid = False
                    actual_code = error.code
                    actual_field = error.field
                self.assertEqual(actual_valid, expect["valid"], case["id"])
                if not expect["valid"]:
                    self.assertEqual(actual_code, expect.get("code"), case["id"])
                    self.assertEqual(actual_field, expect.get("field"), case["id"])


if __name__ == "__main__":
    unittest.main()
