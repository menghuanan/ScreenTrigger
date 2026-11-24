# -*- coding: utf-8 -*-
"""TASK-006 批 B1 契约测试：Python 侧跑 schema/contract-cases.json 共享用例。

三端同源模式（TASK-005 先例扩展，设计 §2.2/§9.1）：
- kind=type_validation：Rust contract_tests.rs 与本文件跑同一份用例——
  同一输入、同一期望（valid/code/field 三元组逐字比对，禁硬编码偏离）；
- kind=config_patch：本批（B1）仅做用例机器可读性/结构校验，
  patch 语义执行方为 B2 的 ConfigService（test_config_service.py）；
  thresholds.configLimits 与 schema/config.schema.json 内嵌块运行时比对防漂移。

运行：python -m unittest backend.tests.test_types_contract -v
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
_CONFIG_SCHEMA_PATH = os.path.join(_REPO_ROOT, "schema", "config.schema.json")


def _load_cases() -> Dict[str, Any]:
    with open(_CASES_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _apply_overrides(case: Dict[str, Any], value: Any) -> Any:
    """用例级覆盖提示（如 name-101：nameOverrideLength 指定 name 实际长度）。"""
    override_length = case.get("nameOverrideLength")
    if override_length is not None and isinstance(value, dict):
        value = dict(value)
        value["name"] = "a" * int(override_length)
    return value


class TestTypeValidationContract(unittest.TestCase):
    """三端共享 type_validation 用例（同一份 JSON，逐字断言）。"""

    def setUp(self) -> None:
        self.document = _load_cases()

    def _type_validation_cases(self) -> List[Dict[str, Any]]:
        return [
            case for case in self.document["cases"]
            if case.get("kind") == "type_validation"
            and "python" in case.get("applies_to", [])
        ]

    def test_type_validation_cases_exist(self) -> None:
        cases = self._type_validation_cases()
        self.assertGreaterEqual(len(cases), 37, "type_validation 用例不得回退")

    def test_shared_type_validation_cases(self) -> None:
        """逐条执行共享用例；valid/code/field 与 expect 逐字一致。"""
        for case in self._type_validation_cases():
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
                self.assertEqual(
                    actual_valid, expect["valid"],
                    "用例 %s valid 判定不一致" % case["id"],
                )
                if not expect["valid"]:
                    self.assertEqual(
                        actual_code, expect.get("code"),
                        "用例 %s 错误码不一致" % case["id"],
                    )
                    self.assertEqual(
                        actual_field, expect.get("field"),
                        "用例 %s 错误字段定位不一致" % case["id"],
                    )


class TestConfigPatchCaseContract(unittest.TestCase):
    """config_patch 用例结构契约（B1 校验机器可读性；执行归 B2 ConfigService）。"""

    def setUp(self) -> None:
        self.document = _load_cases()

    def _config_patch_cases(self) -> List[Dict[str, Any]]:
        return [
            case for case in self.document["cases"]
            if case.get("kind") == "config_patch"
            and "python" in case.get("applies_to", [])
        ]

    def test_config_patch_cases_structure(self) -> None:
        cases = self._config_patch_cases()
        self.assertGreaterEqual(len(cases), 11, "config_patch 用例不得回退")
        for case in cases:
            with self.subTest(case=case["id"]):
                # patch：dict（update）或 null（reset_config 用例）
                self.assertTrue(
                    case.get("patch") is None or isinstance(case.get("patch"), dict),
                    "用例 %s patch 必须为对象或 null" % case["id"],
                )
                self.assertIsInstance(case.get("expectedRevision"), int,
                                      "用例 %s 缺 expectedRevision" % case["id"])
                expect = case.get("expect")
                self.assertIsInstance(expect, dict,
                                      "用例 %s 缺 expect" % case["id"])
                self.assertIsInstance(expect.get("success"), bool,
                                      "用例 %s expect.success 必须为布尔" % case["id"])
                if not expect["success"]:
                    self.assertIsInstance(expect.get("errorCode"), str,
                                          "用例 %s 失败期望缺 errorCode"
                                          % case["id"])
                    self.assertTrue(expect.get("configUnchanged") is True,
                                    "用例 %s 失败必须期望配置不变" % case["id"])
                else:
                    self.assertIsInstance(expect.get("revision"), int,
                                          "用例 %s 成功期望缺 revision"
                                          % case["id"])

    def test_config_limits_no_drift(self) -> None:
        """thresholds.configLimits 与 schema/config.schema.json 内嵌块逐字一致。"""
        with open(_CONFIG_SCHEMA_PATH, "r", encoding="utf-8") as handle:
            config_schema = json.load(handle)
        schema_limits = config_schema.get("configLimits", {})
        contract_limits = self.document.get("thresholds", {}).get("configLimits", {})
        self.assertEqual(
            schema_limits, contract_limits,
            "config.schema.json 与 contract-cases.json 的 configLimits 漂移",
        )


if __name__ == "__main__":
    unittest.main()
