# -*- coding: utf-8 -*-
"""TASK-020 测试替身与通用值构造器（**只依赖标准库 + 基座**）。

提供：

- ``FakeTable`` / ``FakeMeta`` / ``FakeUserdata`` / ``FakeLuaError``：纯 Python 的 Lua 值模型；
- ``FakeLuaInterop``：``LuaInterop`` 端口的纯 Python 实现，使 **JSON 三态算法 / 宿主注册机制**
  在**受管 Python（无 lupa）** 下**真实执行**（不是桩、不是 skip）；
- ``LuaKit``：基于 ``LuaInterop`` **端口**构造 Lua 值的通用夹具——同一套断言在**受管环境**
  （假体）与**探针 venv**（真实 lupa）下**各自真跑**（设计 §8/T02）。

约定拉直（对齐 lupa 2.8 / Lua 5.4 实测）：

- ``lua_type`` 对 nil/boolean/string/number/table/function/userdata 返回权威名；
- ``rawequal`` 对 table/userdata/function 为**身份**比较，对标量为**值**比较；
- ``getmetatable`` 无 metatable 返回 ``None``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Lua 值模型
# ---------------------------------------------------------------------------

class FakeMeta:
    """Lua metatable 替身（``index`` 即 ``__index``）。"""

    def __init__(self, index: Any = None) -> None:
        self.index = index


class FakeTable:
    """Lua table 替身（``data`` = 键→值；``meta`` = metatable）。"""

    def __init__(self, items: Optional[Dict[Any, Any]] = None, meta: Optional[FakeMeta] = None) -> None:
        self.data: Dict[Any, Any] = dict(items) if items else {}
        self.meta: Optional[FakeMeta] = meta


class FakeUserdata:
    """Lua userdata 替身。"""


class FakeLuaError(Exception):
    """假体绑定在失败时抛出的错误（携带环境原生错误值）。"""

    def __init__(self, value: Any) -> None:
        super().__init__("lua error")
        self.value = value


# ---------------------------------------------------------------------------
# FakeLuaInterop
# ---------------------------------------------------------------------------

class FakeLuaInterop:
    """``LuaInterop`` 端口纯 Python 实现（受管环境用）。"""

    def __init__(self) -> None:
        self.cancel_sentinel = FakeTable({"__st_control": "checkpoint_cancelled"})
        self._hook: Any = None

    # -- 类型 / 身份 --------------------------------------------------------

    def lua_type(self, value: Any) -> str:
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, str):
            return "string"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, FakeTable):
            return "table"
        if isinstance(value, FakeUserdata):
            return "userdata"
        if callable(value):
            return "function"
        return "userdata"

    def rawequal(self, a: Any, b: Any) -> bool:
        identity_types = (FakeTable, FakeMeta, FakeUserdata)
        if isinstance(a, identity_types) or isinstance(b, identity_types):
            return a is b
        if callable(a) or callable(b):
            return a is b
        if a is None or b is None:
            return a is None and b is None
        return a == b

    def getmetatable(self, table: Any) -> Any:
        return table.meta if isinstance(table, FakeTable) else None

    def setmetatable(self, table: Any, meta: Any) -> Any:
        if isinstance(table, FakeTable):
            table.meta = meta
        return table

    # -- table --------------------------------------------------------------

    def new_table(self) -> FakeTable:
        return FakeTable()

    def table_keys(self, table: FakeTable) -> List[Any]:
        return list(table.data.keys())

    def table_get(self, table: FakeTable, key: Any) -> Any:
        return table.data.get(key)

    def table_set(self, table: FakeTable, key: Any, value: Any) -> None:
        table.data[key] = value

    def table_len(self, table: FakeTable) -> int:
        length = 0
        while (length + 1) in table.data:
            length += 1
        return length

    # -- 环境 / 加载（假体不支持真实执行） -----------------------------------

    def eval(self, code: str) -> Any:  # pragma: no cover - 受管环境不触达
        raise NotImplementedError("FakeLuaInterop 不支持 eval（真实执行需探针 venv）")

    def new_env(self, whitelist: Any) -> FakeTable:
        return FakeTable(meta=FakeMeta(index=whitelist))

    def load_chunk(self, source: str, chunkname: str, env: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("FakeLuaInterop 不支持 load_chunk")

    def invoke(self, chunk: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("FakeLuaInterop 不支持 invoke")

    def make_host_binding(self, name: str, dispatch: Any) -> Any:
        def binder(*args: Any) -> Any:
            outcome = dispatch(*args)
            if getattr(outcome, "ok", False):
                values = getattr(outcome, "values", ()) or ()
                if len(values) == 1:
                    return values[0]
                return values
            raise FakeLuaError(outcome.error)

        return binder

    def install_hook(self, check: Any, *, mask: str = "crl", count: int = 10000) -> None:
        self._hook = check

    def clear_hook(self) -> None:
        self._hook = None

    def memory_used(self) -> int:
        return 0

    def close(self) -> None:
        self._hook = None


# ---------------------------------------------------------------------------
# 通用值构造器（基于端口；两环境各自真跑）
# ---------------------------------------------------------------------------

class LuaKit:
    """基于 ``LuaInterop`` 端口构造 Lua 值（受管=假体 / 探针=真实 lupa 通用）。"""

    def __init__(self, interop: Any) -> None:
        self.interop = interop

    # 标量
    def nil(self) -> Any:
        return None

    def boolean(self, value: bool) -> Any:
        return bool(value)

    def int(self, value: int) -> Any:
        return int(value)

    def float(self, value: float) -> Any:
        return float(value)

    def string(self, value: str) -> Any:
        return str(value)

    def nan(self) -> Any:
        try:
            return self.interop.eval("0/0")
        except Exception:
            return float("nan")

    def inf(self) -> Any:
        try:
            return self.interop.eval("math.huge")
        except Exception:
            return float("inf")

    def big_int(self) -> Any:
        """2^53（越界整数）；用 Lua 整数字面量以验证按 Python int 判定。"""
        try:
            return self.interop.eval("9007199254740993")  # 2^53+1
        except Exception:
            return 9007199254740993

    # 复合
    def list(self, items: Sequence[Any]) -> Any:
        table = self.interop.new_table()
        for index, item in enumerate(items, 1):
            self.interop.table_set(table, index, item)
        return table

    def object(self, pairs: Dict[Any, Any]) -> Any:
        table = self.interop.new_table()
        for key, value in pairs.items():
            self.interop.table_set(table, key, value)
        return table

    def empty_table(self) -> Any:
        return self.interop.new_table()

    def null(self, sentinels: Any) -> Any:
        return sentinels.null

    def marked_array(self, items: Sequence[Any], sentinels: Any) -> Any:
        table = self.list(items)
        self.interop.setmetatable(table, sentinels.array_mt)
        return table

    def marked_empty_array(self, sentinels: Any) -> Any:
        table = self.interop.new_table()
        self.interop.setmetatable(table, sentinels.array_mt)
        return table

    def function(self) -> Any:
        try:
            return self.interop.eval("(function() end)")
        except Exception:
            return (lambda: None)

    def userdata(self) -> Any:
        try:
            return self.interop.eval("io.stdout")
        except Exception:
            return FakeUserdata()

    def nested_arrays(self, depth: int) -> Any:
        value = self.int(0)
        for _ in range(depth):
            value = self.list([value])
        return value

    def cyclic(self) -> Any:
        table = self.interop.new_table()
        self.interop.table_set(table, 1, table)
        return table
