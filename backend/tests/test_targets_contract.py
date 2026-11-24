# -*- coding: utf-8 -*-
"""TASK-011 契约用例一致性测试（``schema/contract-cases.json`` 三端同源）。

本文件与 ``test_types_contract.py``（kind=type_validation）及 Rust
``contract_tests.rs`` 消费**同一份** ``contract-cases.json``：

- ``kind="type_validation"`` 且 ``typeName="group"``（``applies_to`` 含 python）：
  纯结构校验 ``validate_type("group", value)`` 与 ``expect``（valid/code/field）逐字一致；
- ``kind="dispatch"`` 且 ``command`` 属目标/分组命令（``applies_to`` 含 python）：
  经真实命令层（``Dispatcher`` + ``register_target_commands`` + 注入替身 service）执行，
  断言 TASK-011 §13.1 裁决群组语义（Q1 同层同名、Q2 层级超限、Q3 update_group
  至少给一字段、Q5 显式 null 搬移、Q8 父组不存在）——命令层语义仅 Python 侧可达，
  故这些用例 ``applies_to=["python"]``（Rust 只跑纯 ``type_validation``）。

用例 ``setup`` 支持 ``"$N"`` 占位（引用第 N 个已创建分组的 id），供父组/搬移场景；
每条 dispatch 用例在**全新**临时仓库上执行（setup 之间互不污染）。
运行：python -m unittest backend.tests.test_targets_contract -v
"""

from __future__ import annotations

import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.ports import RawImage  # noqa: E402
from backend.image.store import SessionImageStore  # noqa: E402
from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.storage import DataRoot  # noqa: E402
from backend.storage.imagestore import ImageStore  # noqa: E402
from backend.targets import RecordingCatalogEmitter, Repository, TargetService  # noqa: E402
from backend.targets.commands import register_target_commands  # noqa: E402
from backend.types import TypeValidationError, validate_type  # noqa: E402

_CASES_PATH = os.path.join(_REPO_ROOT, "schema", "contract-cases.json")
_TS = "2026-09-21T09:00:00Z"
SESSION_ID = "s-contract"
# 归属 TASK-011 契约的 group 命令（dispatch 用例 command 白名单）
_GROUP_COMMANDS = {
    "create_group",
    "list_groups",
    "update_group",
    "delete_group",
    "move_target_to_group",
}


def _load_cases() -> Dict[str, Any]:
    with open(_CASES_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _cases_by_kind(document: Dict[str, Any], kind: str, applies_to: str) -> List[Dict[str, Any]]:
    return [
        case for case in document["cases"]
        if case.get("kind") == kind and applies_to in case.get("applies_to", [])
    ]


def _resolve_refs(value: Any, created_ids: List[str]) -> Any:
    """把 ``"$N"`` 占位替换为第 N 个已创建分组的 id（递归 dict/list）。"""
    if isinstance(value, str) and value.startswith("$") and value[1:].isdigit():
        index = int(value[1:])
        return created_ids[index]
    if isinstance(value, dict):
        return {key: _resolve_refs(item, created_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(item, created_ids) for item in value]
    return value


def _dig(data: Any, dotted_path: str) -> Any:
    """按 ``a.b`` 点路径取值（用于 ``expect.result`` 断言）。"""
    current = data
    for part in dotted_path.split("."):
        current = current[part]
    return current


class _Transport:
    """最小传输替身：记录已发信封（单 writer 语义）。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(self, envelope: Dict[str, Any]) -> bool:
        self.sent.append(envelope)
        return True

    def read_line(self) -> Optional[str]:
        return None


class _Session:
    session_id = SESSION_ID


class _Env:
    """单条用例的隔离装配：临时数据根 + 真实命令层（注入替身 service）。"""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="st-contract-")
        self.session = SessionImageStore(session_id=SESSION_ID)
        self.asset_store = ImageStore(self.session, DataRoot(self.dir))
        self.repo = Repository(self.dir, image_store=self.asset_store)
        self.emitter = RecordingCatalogEmitter()
        counter = itertools.count(1)
        self.service = TargetService(
            self.repo,
            self.emitter,
            clock=lambda: _TS,
            id_gen=lambda: "g-%d" % next(counter),
        )
        self.transport = _Transport()
        self.dispatcher = Dispatcher(
            transport=self.transport, session=_Session(), stderr=io.StringIO()
        )
        register_target_commands(self.dispatcher, self.service)
        self.ref = self.session.put(
            RawImage(width=4, height=4, data=bytes([1, 2, 3]) * 16), "png"
        )

    def request(self, command: str, params: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        envelope = {
            "protocol_version": 1,
            "session_id": SESSION_ID,
            "type": "request",
            "id": request_id,
            "command": command,
            "params": params,
        }
        self.dispatcher.dispatch_command(envelope)
        return self.transport.sent[-1]


class GroupTypeValidationContractTest(unittest.TestCase):
    """``type_validation``/``group`` 用例三端同源（Python 侧逐字执行）。"""

    def setUp(self) -> None:
        self.document = _load_cases()

    def _group_cases(self, applies_to: str) -> List[Dict[str, Any]]:
        return [
            case for case in _cases_by_kind(self.document, "type_validation", applies_to)
            if case.get("typeName") == "group"
        ]

    def test_group_type_validation_cases_exist(self) -> None:
        self.assertGreaterEqual(
            len(self._group_cases("python")), 2, "TASK-011 应新增 group 结构校验用例"
        )

    def test_group_cases_are_shared_with_rust(self) -> None:
        """group type_validation 用例须同时声明 rust（三端同源，非 Python 独享）。"""
        self.assertGreaterEqual(
            len(self._group_cases("rust")), 2, "group 结构用例必须 applies_to 含 rust"
        )

    def test_group_type_validation_matches_expect(self) -> None:
        for case in self._group_cases("python"):
            with self.subTest(case=case["id"]):
                expect = case["expect"]
                try:
                    validate_type(case["typeName"], case["value"])
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


class GroupDispatchContractTest(unittest.TestCase):
    """``dispatch``/group 命令用例：TASK-011 裁决语义经真实命令层执行（每条独立仓库）。"""

    def setUp(self) -> None:
        self.document = _load_cases()

    def _group_dispatch_cases(self) -> List[Dict[str, Any]]:
        return [
            case for case in _cases_by_kind(self.document, "dispatch", "python")
            if case.get("command") in _GROUP_COMMANDS
        ]

    def test_group_dispatch_cases_exist(self) -> None:
        self.assertGreaterEqual(
            len(self._group_dispatch_cases()), 4, "TASK-011 应新增 group 命令语义用例"
        )

    def _run_dispatch_case(self, case: Dict[str, Any]) -> Dict[str, Any]:
        env = _Env()  # 每条用例独立仓库，避免 setup 组名相互污染
        created_ids: List[str] = []
        for index, step in enumerate(case.get("setup", [])):
            step_params = _resolve_refs(step["params"], created_ids)
            response = env.request(step["command"], step_params, "setup-%d" % index)
            self.assertTrue(
                response["success"],
                "用例 %s setup 第 %d 步应成功：%r" % (case["id"], index, response),
            )
            created_ids.append(response["data"]["group"]["id"])
        params = _resolve_refs(case["params"], created_ids)
        # nameOverrideLength：按提示覆写 name 长度（镜像 test_types_contract 约定）
        override = case.get("nameOverrideLength")
        if override is not None:
            params = dict(params)
            params["name"] = "a" * int(override)
        return env.request(case["command"], params, "assert")

    def test_group_dispatch_cases(self) -> None:
        for case in self._group_dispatch_cases():
            with self.subTest(case=case["id"]):
                response = self._run_dispatch_case(case)
                expect = case["expect"]
                self.assertEqual(response["success"], expect["success"], case["id"])
                if not expect["success"]:
                    error = response["error"]
                    self.assertEqual(
                        set(error.keys()),
                        {"code", "message", "details", "retryable"},
                        "错误必须为四字段结构",
                    )
                    self.assertEqual(error["code"], expect["errorCode"], case["id"])
                    if "field" in expect:
                        self.assertEqual(
                            error["details"].get("field"), expect["field"], case["id"]
                        )
                else:
                    for path, value in expect.get("result", {}).items():
                        self.assertEqual(_dig(response["data"], path), value, case["id"])


if __name__ == "__main__":
    unittest.main()
