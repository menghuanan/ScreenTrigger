# -*- coding: utf-8 -*-
"""脚本语法校验（TASK-012）：**仅编译、不执行**，lupa 可选、缺失时诚实降级。

设计 §9 要点：

- **仅编译**：调用 Lua ``load(src, chunkname, "t")``（text-only 模式）；**绝不执行**源码、
  不建 VM 环境、不注入 API、不做沙箱（沙箱/执行属 TASK-020）；
- **显式 Lua 5.4**（D06）：使用 ``lupa.lua54`` 运行时，**不得**用 ``lupa`` 默认运行时
  （默认可能是 Lua 5.5 → 放行 5.5 专有语法，与执行引擎 5.4 不一致）；
- ``valid == (len(diagnostics) == 0)``；Lua 解析器遇首个语法错误即停 → 失败时**恰 1 条**诊断；
- ``Diagnostic = {line:int≥1, column:int≥1|null, message:string}``：
  - ``line`` 从 Lua 错误串解析（1 起）；
  - ``column`` **只在能从错误文本可靠推导时给出**：``message`` 匹配 ``near '<token>'``
    且该 token 在所报行内**唯一出现**，才给出 ``index+1``；否则 **``None``**（**严禁猜测**，
    对齐 02「引擎无法判断时为 null，不编造」）；
- **lupa 不可用 → 抛 ``ENGINE_UNAVAILABLE``（``retryable=False``，无 details）**，
  绝**不**返回 ``valid:true``（对齐 TASK-009「缺 Pillow → JPEG ENGINE_UNAVAILABLE」先例）。
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from typing import Any, List, Optional

from ..storage.errors import StorageError

# 降级错误（Q1：码=ENGINE_UNAVAILABLE、retryable=False、无 details）。
LUA_UNAVAILABLE_CODE: str = "ENGINE_UNAVAILABLE"
LUA_UNAVAILABLE_MESSAGE: str = (
    "当前引擎未安装 Lua 5.4 编译器（lupa.lua54），无法进行脚本语法校验"
)

# package 名（探针 venv 提供 lupa 2.8，含 lua54；受管 Python 无）。
_LUPA_MODULE: str = "lupa"
# **必须显式 lua54**（设计 §9.3 / D06：对齐 `main.py:detect_lua_version`，不得用
# lupa 默认运行时——默认后端可能是 Lua 5.5，会放行 5.5 专有语法，而执行引擎按 5.4，
# 导致运行期才报错，正是本功能应拦截的情形）。
_LUPA_54_MODULE: str = "lupa.lua54"

# Lua 错误串形如 `<chunkname>:<line>: <message>`；chunkname 已净化（无冒号）→
# 非贪婪匹配首个 `:<digits>:` 即真实行号标记。
_ERROR_RE = re.compile(r"^.*?:(\d+):\s*(.*)$", re.S)
# `near '<token>'` 定位（Q4：仅此形态才尝试推导列号）。
_NEAR_RE = re.compile(r"near '([^']*)'")
_CHUNKNAME_SANITIZE_RE = re.compile(r"[^0-9A-Za-z_-]")


@dataclass(frozen=True)
class Diagnostic:
    """一条语法诊断（02/§9.1）：line 1 起、column 1 起或 null、message 描述文本。"""

    line: int
    column: Optional[int]
    message: str

    def to_dict(self) -> dict:
        return {"line": self.line, "column": self.column, "message": self.message}


def lupa_available() -> bool:
    """lupa 的 **Lua 5.4** 编译能力是否可导入（不触发导入副作用）。

    须同时具备 ``lupa`` 与 ``lupa.lua54``（D06：显式 5.4，不得用默认运行时）。
    """
    return (
        importlib.util.find_spec(_LUPA_MODULE) is not None
        and importlib.util.find_spec(_LUPA_54_MODULE) is not None
    )


def engine_unavailable() -> StorageError:
    """lupa 缺失的降级错误（Q1：retryable=False、无 details）。"""
    return StorageError(LUA_UNAVAILABLE_CODE, LUA_UNAVAILABLE_MESSAGE, retryable=False)


class ScriptValidator:
    """语法校验端口（可注入替身）。``compile`` 返回诊断列表（空=合法）。"""

    def compile(self, source: str, name: str = "script") -> List[Diagnostic]:  # pragma: no cover
        raise NotImplementedError


class LupaScriptValidator(ScriptValidator):
    """基于 lupa **Lua 5.4**（``lupa.lua54``）的**仅编译**校验器；缺失 → ``ENGINE_UNAVAILABLE``。"""

    def compile(self, source: str, name: str = "script") -> List[Diagnostic]:
        lua54 = self._import_lua54()
        try:
            runtime = lua54.LuaRuntime()
            loader = runtime.eval(
                "function(src, cname)\n"
                '  local f, err = load(src, cname, "t")\n'
                "  if f ~= nil then return { ok = true } end\n"
                "  return { ok = false, err = err }\n"
                "end"
            )
            result = loader(source, self._chunkname(name))
        except StorageError:
            raise
        except Exception as exc:  # Lua 运行时/桥接故障：引擎无法提供编译能力 → 降级
            raise engine_unavailable() from exc

        if bool(result["ok"]):
            return []
        message_text = str(result["err"])
        line, message = self._split_error(message_text)
        return [Diagnostic(line=line, column=self._derive_column(message, source, line), message=message)]

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _import_lua54() -> Any:
        """导入 **Lua 5.4** 运行时模块（``lupa.lua54``）；缺失 → ``ENGINE_UNAVAILABLE``。

        D06：引擎显式使用 Lua 5.4；不得回退到 ``lupa`` 默认运行时（可能是 5.5），
        否则语法闸门与执行引擎版本不一致。
        """
        try:
            import lupa.lua54 as lua54  # noqa: F401  (惰性：仅 validate_script 路径触发)
        except ImportError as exc:
            raise engine_unavailable() from exc
        if getattr(lua54, "LuaRuntime", None) is None:
            raise engine_unavailable()
        return lua54

    @staticmethod
    def _chunkname(name: str) -> str:
        """净化 chunkname（去冒号/换行/非 ASCII）→ ``@<safe>``，使错误串可稳定解析。"""
        safe = _CHUNKNAME_SANITIZE_RE.sub("_", name or "")
        safe = safe.strip("_")[:48] or "script"
        return "@" + safe

    @staticmethod
    def _split_error(errmsg: str) -> tuple:
        """从 Lua 错误串解析 ``(line, message)``；无法解析时行号保守取 1。"""
        match = _ERROR_RE.match(errmsg.strip())
        if match is None:
            return 1, errmsg.strip()
        return int(match.group(1)), match.group(2).strip()

    @staticmethod
    def _derive_column(message: str, source: str, line: int) -> Optional[int]:
        """best-effort 列号（Q4）：``near '<token>'`` 且 token 在该行唯一 → index+1；否则 None。"""
        match = _NEAR_RE.search(message)
        if match is None:
            return None
        token = match.group(1)
        if token == "":
            return None
        lines = source.split("\n")
        if not 1 <= line <= len(lines):
            return None
        line_text = lines[line - 1]
        if line_text.count(token) == 1:
            return line_text.index(token) + 1
        return None


def build_default_script_validator() -> ScriptValidator:
    """生产默认校验器（lupa 实现；缺 lupa 时于 compile 期抛 ``ENGINE_UNAVAILABLE``）。"""
    return LupaScriptValidator()
