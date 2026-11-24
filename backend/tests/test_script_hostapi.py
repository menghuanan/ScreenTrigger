# -*- coding: utf-8 -*-
"""TASK-020 宿主函数注册机制单测（backend/script/hostapi.py）。

受管 Python（无 lupa）用 ``FakeLuaInterop`` **真实执行**注册器逻辑：前后检查、值复制（不共享
table）、异常归一为 Lua 错误表、``json_array``/``json_null`` 语义、pcall 不可绕过（闸门关闭后
每次调用都再抛）。覆盖 TEST-008 / TEST-009 中属本任务的注册器子场景。
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.script.cancel import CancelFlag, ExecutionGate  # noqa: E402
from backend.script.hostapi import HostApiRegistry  # noqa: E402
from backend.script.json_bridge import JsonBridge, make_sentinels  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.tests._script_lua_fakes import FakeLuaInterop  # noqa: E402


class HostApiRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.interop = FakeLuaInterop()
        self.sentinels = make_sentinels(self.interop)
        self.bridge = JsonBridge(self.interop, self.sentinels)
        self.flag = CancelFlag()
        self.gate = ExecutionGate(cancel=self.flag)
        self.registry = HostApiRegistry(self.gate, self.bridge)
        self.registry.install_json_markers()

    def _table(self, pairs):
        tbl = self.interop.new_table()
        for key, value in pairs.items():
            self.interop.table_set(tbl, key, value)
        return tbl

    # -- 值复制 / 正常路径 ---------------------------------------------------

    def test_wrap_copies_arguments_and_result(self):
        captured = {}

        def capture(value):
            captured["arg"] = value
            return {"echo": value}

        self.registry.register("capture", capture)
        dispatch = self.registry.bindings()["capture"]
        lua_arg = self._table({"a": 1})
        outcome = dispatch(lua_arg)
        self.assertTrue(outcome.ok)
        # 入参被复制为 JSON 值（Python dict），不是 Lua 对象
        self.assertEqual(captured["arg"], {"a": 1})
        self.assertIsInstance(captured["arg"], dict)
        # 返回值是全新 Lua 值（不共享）
        self.assertFalse(self.interop.rawequal(outcome.values[0], lua_arg))
        self.assertEqual(self.bridge.lua_to_json(outcome.values[0]), {"echo": {"a": 1}})

    def test_multiple_return_values_forwarded(self):
        self.registry.register("pair", lambda: (1, 2))
        outcome = self.registry.bindings()["pair"]()
        self.assertTrue(outcome.ok)
        self.assertEqual(len(outcome.values), 2)

    # -- 前后检查 ------------------------------------------------------------

    def test_before_checkpoint_blocks_when_cancelled(self):
        called = {"n": 0}

        def fn():
            called["n"] += 1
            return 1

        self.registry.register("fn", fn)
        self.flag.cancel()
        outcome = self.registry.bindings()["fn"]()
        self.assertFalse(outcome.ok)
        self.assertEqual(called["n"], 0)  # 取消后**不**调用业务
        self.assertTrue(self.interop.rawequal(self.sentinels.null, self.sentinels.null))
        self.assertTrue(self.interop.rawequal(self.interop.cancel_sentinel, outcome.error))

    def test_after_checkpoint_blocks_when_cancelled_during_call(self):
        def cancelling():
            self.flag.cancel()
            return 1

        self.registry.register("cancelling", cancelling)
        outcome = self.registry.bindings()["cancelling"]()
        self.assertFalse(outcome.ok)
        self.assertTrue(self.interop.rawequal(self.interop.cancel_sentinel, outcome.error))

    def test_pcall_cannot_bypass_gate(self):
        # 闸门保持关闭：即使第一次被捕获，后续调用仍再抛（每次调用前置检查）
        self.registry.register("fn", lambda: 1)
        self.flag.cancel()
        dispatch = self.registry.bindings()["fn"]
        for _ in range(3):
            outcome = dispatch()
            self.assertFalse(outcome.ok)
            self.assertTrue(self.interop.rawequal(self.interop.cancel_sentinel, outcome.error))

    # -- 异常归一 ------------------------------------------------------------

    def test_storage_error_normalized_to_lua_error_table(self):
        def failing():
            raise StorageError("INVALID_PARAMS", "坏参数", field="x")

        self.registry.register("failing", failing)
        outcome = self.registry.bindings()["failing"]()
        self.assertFalse(outcome.ok)
        table = outcome.error
        self.assertEqual(table.data["code"], "INVALID_PARAMS")
        self.assertEqual(table.data["message"], "坏参数")
        self.assertEqual(table.data["details"].data.get("field"), "x")
        # 错误表是普通 Lua 表，绝无 Python 异常对象
        self.assertEqual(self.interop.lua_type(table), "table")

    def test_unexpected_exception_becomes_internal_error(self):
        def boom():
            raise RuntimeError("炸了")

        self.registry.register("boom", boom)
        outcome = self.registry.bindings()["boom"]()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.data["code"], "INTERNAL_ERROR")

    # -- json_array / json_null ----------------------------------------------

    def test_install_json_markers_registers_literal_and_function(self):
        literals = self.registry.literals()
        self.assertIn("json_null", literals)
        self.assertTrue(self.interop.rawequal(self.sentinels.null, literals["json_null"]))
        self.assertIn("json_array", self.registry.bindings())

    def test_json_array_marks_contiguous_table(self):
        dispatch = self.registry.bindings()["json_array"]
        outcome = dispatch(self._table({1: 10, 2: 20, 3: 30}))
        self.assertTrue(outcome.ok)
        marked = outcome.values[0]
        self.assertTrue(
            self.interop.rawequal(
                self.sentinels.array_mt, self.interop.getmetatable(marked)
            )
        )
        self.assertEqual(self.bridge.lua_to_json(marked), [10, 20, 30])

    def test_json_array_empty_is_array(self):
        dispatch = self.registry.bindings()["json_array"]
        outcome = dispatch(self.interop.new_table())
        self.assertTrue(outcome.ok)
        self.assertEqual(self.bridge.lua_to_json(outcome.values[0]), [])

    def test_json_array_rejects_non_contiguous(self):
        dispatch = self.registry.bindings()["json_array"]
        outcome = dispatch(self._table({1: 10, 3: 30}))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.data["code"], "INVALID_PARAMS")

    def test_json_array_rejects_non_table(self):
        dispatch = self.registry.bindings()["json_array"]
        outcome = dispatch("not a table")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error.data["code"], "INVALID_PARAMS")


if __name__ == "__main__":
    unittest.main()
