# -*- coding: utf-8 -*-
"""TASK-020 JSON 桥单测（backend/script/json_bridge.py）。

**双环境真跑**（设计 §8/T02，禁 skipTest）：

- 受管 Python（无 lupa）→ 用 ``FakeLuaInterop``（纯 Python）**真实执行**三态/拒绝/深度/大小/
  循环算法；
- 探针 venv（有 lupa/Lua 5.4）→ 用真实 ``LupaInterop`` 对**同一批断言**真跑。

覆盖 TEST-010 中属本任务的子场景：对象/数组/空对象/空数组/null/Unicode/有限浮点/嵌套往返、
函数/循环 table/NaN/混合键/稀疏/越界拒绝、零/一个/多个返回值语义、拒绝在副作用前。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.script.interop import LupaInterop  # noqa: E402
from backend.script.json_bridge import (  # noqa: E402
    MAX_ENCODED_BYTES,
    MAX_SAFE_INT,
    JsonBridge,
    make_sentinels,
)
from backend.storage.errors import StorageError  # noqa: E402
from backend.tests._script_lua_fakes import FakeLuaInterop, LuaKit  # noqa: E402

HAS_LUPA = (
    importlib.util.find_spec("lupa") is not None
    and importlib.util.find_spec("lupa.lua54") is not None
)


class JsonBridgeTest(unittest.TestCase):
    """同一套断言在受管环境（假体）与探针 venv（真 lupa）各自真跑。"""

    def setUp(self) -> None:
        if HAS_LUPA:
            self.interop = LupaInterop()
            self.interop.build()
            self.env_name = "lupa"
        else:
            self.interop = FakeLuaInterop()
            self.env_name = "fake"
        self.sentinels = make_sentinels(self.interop)
        self.bridge = JsonBridge(self.interop, self.sentinels)
        self.kit = LuaKit(self.interop)

    def tearDown(self) -> None:
        close = getattr(self.interop, "close", None)
        if close is not None and HAS_LUPA:
            close()

    # -- 工具 ----------------------------------------------------------------

    def _assert_rejects(self, value, *, code="SERIALIZATION_ERROR"):
        with self.assertRaises(StorageError) as ctx:
            self.bridge.lua_to_json(value)
        self.assertEqual(ctx.exception.code, code)
        self.assertFalse(ctx.exception.retryable)
        return ctx.exception

    # -- 标量 / 三态 ---------------------------------------------------------

    def test_scalars(self):
        self.assertIsNone(self.bridge.lua_to_json(self.kit.nil()))
        self.assertTrue(self.bridge.lua_to_json(self.kit.boolean(True)))
        self.assertFalse(self.bridge.lua_to_json(self.kit.boolean(False)))
        self.assertEqual(self.bridge.lua_to_json(self.kit.int(42)), 42)
        self.assertEqual(self.bridge.lua_to_json(self.kit.float(1.5)), 1.5)
        self.assertEqual(self.bridge.lua_to_json(self.kit.string("hi")), "hi")

    def test_unicode_roundtrip(self):
        text = "开始按钮 → 🚀"
        self.assertEqual(self.bridge.lua_to_json(self.kit.string(text)), text)

    def test_empty_table_is_object(self):
        self.assertEqual(self.bridge.lua_to_json(self.kit.empty_table()), {})

    def test_json_array_empty_is_list(self):
        marked = self.kit.marked_empty_array(self.sentinels)
        self.assertEqual(self.bridge.lua_to_json(marked), [])

    def test_array_and_object(self):
        self.assertEqual(self.bridge.lua_to_json(self.kit.list([1, 2, 3])), [1, 2, 3])
        self.assertEqual(
            self.bridge.lua_to_json(self.kit.object({"a": 1, "b": "x"})),
            {"a": 1, "b": "x"},
        )

    def test_marked_array_of_values(self):
        marked = self.kit.marked_array([1, 2, 3], self.sentinels)
        self.assertEqual(self.bridge.lua_to_json(marked), [1, 2, 3])

    def test_json_null_in_object_and_array(self):
        obj = self.kit.object({"a": 1, "b": self.sentinels.null})
        self.assertEqual(self.bridge.lua_to_json(obj), {"a": 1, "b": None})
        arr = self.kit.list([1, self.sentinels.null, 3])
        self.assertEqual(self.bridge.lua_to_json(arr), [1, None, 3])

    def test_nested_roundtrip(self):
        value = self.kit.object(
            {
                "list": self.kit.list([1, self.kit.object({"deep": True})]),
                "null": self.sentinels.null,
            }
        )
        self.assertEqual(
            self.bridge.lua_to_json(value), {"list": [1, {"deep": True}], "null": None}
        )

    # -- 拒绝 ----------------------------------------------------------------

    def test_function_rejected(self):
        self._assert_rejects(self.kit.function())

    def test_userdata_rejected(self):
        self._assert_rejects(self.kit.userdata())

    def test_mixed_keys_rejected(self):
        tbl = self.interop.new_table()
        self.interop.table_set(tbl, 1, "a")
        self.interop.table_set(tbl, "k", "b")
        self._assert_rejects(tbl)

    def test_sparse_array_rejected(self):
        tbl = self.interop.new_table()
        self.interop.table_set(tbl, 1, "a")
        self.interop.table_set(tbl, 3, "c")
        self._assert_rejects(tbl)

    def test_cyclic_table_rejected(self):
        self._assert_rejects(self.kit.cyclic())

    def test_nan_rejected(self):
        self._assert_rejects(self.kit.nan())

    def test_infinity_rejected(self):
        self._assert_rejects(self.kit.inf())

    def test_integer_beyond_safe_range_rejected(self):
        value = self.kit.big_int()
        self.assertGreater(abs(int(value)), MAX_SAFE_INT)
        self._assert_rejects(value)

    def test_reject_does_not_mutate_source(self):
        # 拒绝在产生副作用前：混合键表在拒绝后键集合不变（桥只读遍历）
        tbl = self.interop.new_table()
        self.interop.table_set(tbl, 1, "a")
        self.interop.table_set(tbl, "k", "b")
        before = sorted(str(k) for k in self.interop.table_keys(tbl))
        self._assert_rejects(tbl)
        after = sorted(str(k) for k in self.interop.table_keys(tbl))
        self.assertEqual(before, after)

    # -- 深度 / 大小 ---------------------------------------------------------

    def test_depth_limit_32_ok_33_rejected(self):
        value = self.bridge.lua_to_json(self.kit.nested_arrays(32))
        for _ in range(32):
            self.assertIsInstance(value, list)
            self.assertEqual(len(value), 1)
            value = value[0]
        self.assertEqual(value, 0)
        self._assert_rejects(self.kit.nested_arrays(33))

    def test_depth_rejection_is_serialization_error_not_depth_exceeded(self):
        # 设计 §6 Q2 裁决：JSON 嵌套 >32 → SERIALIZATION_ERROR（非 CALL_DEPTH_EXCEEDED）
        exc = self._assert_rejects(self.kit.nested_arrays(33))
        self.assertEqual(exc.code, "SERIALIZATION_ERROR")
        self.assertNotEqual(exc.code, "CALL_DEPTH_EXCEEDED")

    def test_encoded_size_limit(self):
        ok_text = "a" * (MAX_ENCODED_BYTES - 100)
        self.assertEqual(self.bridge.lua_to_json(self.kit.string(ok_text)), ok_text)
        big_text = "a" * (MAX_ENCODED_BYTES + 100)
        exc = self._assert_rejects(self.kit.string(big_text))
        self.assertEqual(exc.details.get("field"), "value")
        self.assertGreater(exc.details.get("bytes", 0), MAX_ENCODED_BYTES)
        self.assertEqual(exc.details.get("limit"), MAX_ENCODED_BYTES)

    # -- bridge_return 零/一个/多个 ------------------------------------------

    def test_bridge_return_semantics(self):
        self.assertIsNone(self.bridge.bridge_return([]))  # 零 → null
        self.assertEqual(self.bridge.bridge_return([self.kit.int(7)]), 7)  # 一个 → 转换
        with self.assertRaises(StorageError) as ctx:  # 多个 → 拒绝
            self.bridge.bridge_return([self.kit.int(1), self.kit.int(2)])
        self.assertEqual(ctx.exception.code, "SERIALIZATION_ERROR")
        self.assertEqual(ctx.exception.details.get("field"), "return")

    # -- json_to_lua（值复制、不共享；null → 哨兵） --------------------------

    def test_json_to_lua_null_sentinel(self):
        produced = self.bridge.json_to_lua(None)
        self.assertTrue(self.interop.rawequal(self.sentinels.null, produced))

    def test_json_to_lua_round_trips_and_is_not_shared(self):
        source = {"a": [1, None, 3], "b": {"c": "x"}}
        first = self.bridge.json_to_lua(source)
        second = self.bridge.json_to_lua(source)
        self.assertFalse(self.interop.rawequal(first, second))  # 不共享
        self.assertEqual(self.bridge.lua_to_json(first), source)
        self.assertEqual(self.bridge.lua_to_json(second), source)

    def test_json_to_lua_rejects_non_json(self):
        with self.assertRaises(StorageError):
            self.bridge.json_to_lua(object())


if __name__ == "__main__":
    unittest.main()
