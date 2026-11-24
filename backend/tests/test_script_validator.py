# -*- coding: utf-8 -*-
"""TASK-012 语法校验器单测（backend/script/validator.py）：**仅编译、不执行**。

诚实分环境（设计 §9.4/§13.2，**禁止 skipTest 静默通过**）：

- 受管 Python（**无 lupa**）→ 断言 ``validate_script`` 诚实降级为 ``ENGINE_UNAVAILABLE``
  （``retryable=False``、无 details），**绝不**返回 ``valid:true``；
- 探针 venv（**有 lupa**）→ 真编译断言（``valid=false``、``line`` 正确、诊断**恰 1 条**、
  ``column`` 仅在能**无歧义**定位时给出否则为 ``null``）。

同一份测试在两种环境下**各自真实执行对应分支**（须同时具备 ``lupa`` 与 ``lupa.lua54``——
以 ``importlib.util.find_spec("lupa")`` 与 ``importlib.util.find_spec("lupa.lua54")`` 判定，
D06：显式 Lua 5.4，不得用默认运行时），不出现跳过。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.script.validator import (  # noqa: E402
    LUA_UNAVAILABLE_CODE,
    Diagnostic,
    build_default_script_validator,
    engine_unavailable,
    lupa_available,
)
from backend.storage.errors import StorageError  # noqa: E402

# 环境能力判定（受管 Python 无 lupa；探针 venv 有 lupa/Lua 5.4）。
# 须同时具备 lupa 与 lupa.lua54（D06：显式 5.4，不得用默认运行时）。
HAS_LUPA = (
    importlib.util.find_spec("lupa") is not None
    and importlib.util.find_spec("lupa.lua54") is not None
)


def _assert_engine_unavailable(test: unittest.TestCase, exc: StorageError) -> None:
    test.assertEqual(exc.code, LUA_UNAVAILABLE_CODE)
    test.assertEqual(exc.code, "ENGINE_UNAVAILABLE")
    test.assertFalse(exc.retryable)
    test.assertEqual(exc.details, {})


class ScriptValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = build_default_script_validator()

    def test_availability_flag_matches_find_spec(self):
        self.assertEqual(lupa_available(), HAS_LUPA)

    def test_engine_unavailable_shape_is_env_independent(self):
        # 不管环境如何，降级错误本身必须是四字段：retryable=False、无 details（Q1）
        err = engine_unavailable()
        _assert_engine_unavailable(self, err)

    def test_diagnostic_to_dict(self):
        with_col = Diagnostic(line=12, column=5, message="m")
        self.assertEqual(with_col.to_dict(), {"line": 12, "column": 5, "message": "m"})
        without_col = Diagnostic(line=1, column=None, message="m")
        self.assertIsNone(without_col.to_dict()["column"])

    def test_valid_source(self):
        if not HAS_LUPA:
            with self.assertRaises(StorageError) as ctx:
                self.validator.compile("return 1")
            _assert_engine_unavailable(self, ctx.exception)
            return
        self.assertEqual(self.validator.compile("return 1"), [])

    def test_syntax_error_line_and_single_diagnostic(self):
        if not HAS_LUPA:
            with self.assertRaises(StorageError) as ctx:
                self.validator.compile("local x =\n")
            _assert_engine_unavailable(self, ctx.exception)
            return
        diagnostics = self.validator.compile("local x =\n")
        self.assertEqual(len(diagnostics), 1)  # Lua 首个错误即停 → 恰 1 条
        self.assertEqual(diagnostics[0].line, 2)
        self.assertIn("unexpected symbol", diagnostics[0].message)
        # 'near <eof>' 无引号 token → 无法无歧义定位列 → null（严禁猜测）
        self.assertIsNone(diagnostics[0].column)

    def test_column_derived_only_when_token_unique_on_line(self):
        if not HAS_LUPA:
            with self.assertRaises(StorageError) as ctx:
                self.validator.compile("if then end")
            _assert_engine_unavailable(self, ctx.exception)
            return
        # 'then' 在第 1 行唯一出现 → column = index+1 = 4
        unique = self.validator.compile("if then end")
        self.assertEqual(len(unique), 1)
        self.assertEqual(unique[0].line, 1)
        self.assertEqual(unique[0].column, 4)
        # '=' 在第 2 行出现两次 → 不可无歧义定位 → null（不得用 1 或任何猜测值冒充）
        duplicated = self.validator.compile("local a=1\nx = = 2\n")
        self.assertEqual(len(duplicated), 1)
        self.assertEqual(duplicated[0].line, 2)
        self.assertIsNone(duplicated[0].column)

    def test_compile_does_not_execute_source(self):
        # 若被执行会抛 Lua 运行期错误；仅编译 → 语法合法、无诊断
        if not HAS_LUPA:
            with self.assertRaises(StorageError) as ctx:
                self.validator.compile('error("boom")')
            _assert_engine_unavailable(self, ctx.exception)
            return
        self.assertEqual(self.validator.compile('error("boom")'), [])

    def test_uses_lua54_backend_not_default(self):
        # F-1 回归：validator 必须**显式 lua54**；'global x = 1' 在 Lua 5.4 非法、
        # 在 Lua 5.5（lupa 默认运行时）合法。若误用默认运行时，本例会得到 [] 而失败。
        if not HAS_LUPA:
            with self.assertRaises(StorageError) as ctx:
                self.validator.compile("global x = 1")
            _assert_engine_unavailable(self, ctx.exception)
            return
        diagnostics = self.validator.compile("global x = 1")
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].line, 1)

    def test_binary_chunk_rejected_by_text_mode(self):
        # load(..., "t") 拒绝二进制/预编译 chunk → 视为语法错误（非执行）
        if not HAS_LUPA:
            with self.assertRaises(StorageError) as ctx:
                self.validator.compile("\x1bLuaQ")
            _assert_engine_unavailable(self, ctx.exception)
            return
        diagnostics = self.validator.compile("\x1bLuaQ")
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("binary chunk", diagnostics[0].message)


if __name__ == "__main__":
    unittest.main()
