# -*- coding: utf-8 -*-
"""Lua 运行时互通端口与真实实现（TASK-020 §4.2 interop.py）。

端口 / 适配器（Ports & Adapters，设计 §2.5）：

- ``LuaInterop``：JSON 桥 / VM 所需的最小 Lua 原语（Protocol，可注入替身）；
- ``LupaInterop``：基于 ``lupa.lua54``（**显式 Lua 5.4**，D06）的真实实现；惰性导入，
  缺失或版本不符 → ``ENGINE_UNAVAILABLE``（复用 ``validator.engine_unavailable()``）。

**已验证运行时事实**（探针 venv，lupa 2.8 / Lua 5.4；设计 §2.4）：

- runtime type 只能用 ``type_fn = rt.eval("type")``；默认 ``lupa.lua_type`` 解析到 lua55
  对 lua54 包装对象恒返回 ``None``，``lupa.lua54.lua_type`` 对标量返回 ``None``——故
  ``LuaInterop.lua_type`` 一律经 ``type_fn``（§2.4 ①）；
- 身份/循环/哨兵一律用 ``rawequal``（§2.4 ②：lupa 每次访问生成新包装对象，``id()``/``==``
  失效）；
- ``debug.sethook`` 只接受 Lua 函数，Python 回调须用 Lua 闭包包装（§2.4 ⑦）；且 hook **不**
  直接抛 Python 异常（避免把 Python 对象泄漏给用户 ``pcall`` 反射），改为经 Lua 侧 ``error``
  抛宿主私有哨兵（控制信号）。

**不 import backend.vision**（守 ``Script`` 模块边界）；**零新增第三方依赖**（仅 lupa 2.8 +
标准库）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, List, NamedTuple, Optional, Protocol, runtime_checkable

from .validator import engine_unavailable

#: 宿主私有 debug 引用安装的 hook 掩码（call/return/line + count；count 走单独参数）。
DEFAULT_HOOK_MASK: str = "crl"
#: count hook 间隔（07§3：从 10000 指令评估，不承诺固定耗时）。
DEFAULT_COUNT_INTERVAL: int = 10000


class LuaChunkSyntaxError(Exception):
    """``load`` 失败（语法/二进制 chunk）——携带 Lua 原始错误串，供 VM 解析行列。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RunResult(NamedTuple):
    """一次 chunk 执行的结果（``invoke`` 返回；错误串由 VM 决定归因）。"""

    ok: bool
    values: tuple
    error: str
    error_value: Any = None


@runtime_checkable
class LuaInterop(Protocol):
    """Lua 原语端口（可注入 ``FakeLuaInterop`` 于受管 Python 真跑算法）。"""

    def lua_type(self, value: Any) -> str:  # 权威类型名（§2.4 ①）
        ...

    def rawequal(self, a: Any, b: Any) -> bool:  # 唯一可靠身份（§2.4 ②）
        ...

    def getmetatable(self, table: Any) -> Any:
        ...

    def setmetatable(self, table: Any, meta: Any) -> Any:
        ...

    def new_table(self) -> Any:
        ...

    def table_keys(self, table: Any) -> List[Any]:
        ...

    def table_get(self, table: Any, key: Any) -> Any:
        ...

    def table_set(self, table: Any, key: Any, value: Any) -> None:
        ...

    def table_len(self, table: Any) -> int:
        ...

    def eval(self, code: str) -> Any:
        ...

    def new_env(self, whitelist: Any) -> Any:
        ...

    def load_chunk(self, source: str, chunkname: str, env: Any) -> Any:
        ...

    def invoke(self, chunk: Any) -> RunResult:
        ...

    def make_host_binding(self, name: str, dispatch: Callable[..., Any]) -> Any:
        ...

    def install_hook(
        self, check: Callable[..., bool], *, mask: str = DEFAULT_HOOK_MASK,
        count: int = DEFAULT_COUNT_INTERVAL,
    ) -> None:
        ...

    def clear_hook(self) -> None:
        ...

    def memory_used(self) -> int:
        ...

    def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# 真实实现
# ---------------------------------------------------------------------------


class LupaInterop:
    """基于 ``lupa.lua54`` 的 ``LuaInterop`` 实现（缺失 → ``ENGINE_UNAVAILABLE``）。"""

    def __init__(self, *, max_memory_mb: int = 128) -> None:
        if max_memory_mb <= 0:
            raise ValueError("max_memory_mb 必须为正")
        self._max_memory_mb = int(max_memory_mb)
        self._rt: Any = None
        self.cancel_sentinel: Any = None  # 宿主私有取消控制哨兵（表；非 06 错误码）

    # -- 生命周期 ------------------------------------------------------------

    def build(self) -> None:
        """导入 ``lupa.lua54``、创建受限 VM、校验 ``_VERSION``，并固化私有原语。

        Raises:
            StorageError: ``ENGINE_UNAVAILABLE``（lupa 缺失或非 Lua 5.4；retryable=False、
                无 details）。
        """
        lua54 = self._import_lua54()
        try:
            self._rt = lua54.LuaRuntime(max_memory=self._max_memory_mb * 1024 * 1024)
        except Exception as exc:  # 构造失败 → 诚实降级（不伪造可用）
            raise engine_unavailable() from exc
        self._lua_error = lua54.LuaError
        if self.verify_version() != "Lua 5.4":
            raise engine_unavailable()
        # 宿主私有原语（不进用户 env）
        self._type_fn = self._rt.eval("type")
        self._invoke_fn = self._rt.eval("function(f) return table.pack(pcall(f)) end")
        self._rawequal_fn = self._rt.eval("rawequal")
        self._getmetatable_fn = self._rt.eval("getmetatable")
        self._setmetatable_fn = self._rt.eval("setmetatable")
        self._sethook_fn = self._rt.eval("debug.sethook")
        self._make_env_fn = self._rt.eval(
            "function(wl) return setmetatable({}, {__index = wl}) end"
        )
        self._load_fn = self._rt.eval(
            "function(src, name, env)\n"
            '  local f, err = load(src, name, "t", env)\n'
            "  if f == nil then return { ok = false, err = err } end\n"
            "  return { ok = true, fn = f }\n"
            "end"
        )
        self._make_binding_fn = self._rt.eval(
            "function(name, fn)\n"
            "  return function(...)\n"
            "    local packed = table.pack(...)\n"
            "    local res = fn(packed)\n"
            "    if res.ok then\n"
            "      local n = res.n or 0\n"
            "      if n == 0 then return end\n"
            "      return table.unpack(res.values, 1, n)\n"
            "    end\n"
            "    error(res.error, 0)\n"
            "  end\n"
            "end"
        )
        self._make_hook_fn = self._rt.eval(
            "function(check, sentinel)\n"
            "  return function(ev, line)\n"
            "    if check(ev, line) then error(sentinel, 0) end\n"
            "  end\n"
            "end"
        )
        # 宿主私有取消哨兵：仅用于从 Hook 以 Lua error 形式抛出控制信号
        # （不抛 Python 异常，避免把 Python 对象交给用户 pcall 反射）。
        self.cancel_sentinel = self._rt.eval("({ __st_control = 'checkpoint_cancelled' })")

    def verify_version(self) -> str:
        """读 ``_VERSION``（探针实证 == ``"Lua 5.4"``）。"""
        return str(self._rt.eval("_VERSION"))

    # -- 原语 ----------------------------------------------------------------

    def lua_type(self, value: Any) -> str:
        result = self._type_fn(value)
        return str(result) if result is not None else "nil"

    def rawequal(self, a: Any, b: Any) -> bool:
        return bool(self._rawequal_fn(a, b))

    def getmetatable(self, table: Any) -> Any:
        return self._getmetatable_fn(table)

    def setmetatable(self, table: Any, meta: Any) -> Any:
        self._setmetatable_fn(table, meta)
        return table

    def new_table(self) -> Any:
        return self._rt.table()

    def table_keys(self, table: Any) -> List[Any]:
        return list(table.keys())

    def table_get(self, table: Any, key: Any) -> Any:
        return table[key]

    def table_set(self, table: Any, key: Any, value: Any) -> None:
        table[key] = value

    def table_len(self, table: Any) -> int:
        return len(table)

    def eval(self, code: str) -> Any:
        return self._rt.eval(code)

    def new_env(self, whitelist: Any) -> Any:
        return self._make_env_fn(whitelist)

    def load_chunk(self, source: str, chunkname: str, env: Any) -> Any:
        result = self._load_fn(source, chunkname, env)
        if not bool(result["ok"]):
            raise LuaChunkSyntaxError(str(result["err"]))
        return result["fn"]

    def invoke(self, chunk: Any) -> RunResult:
        try:
            returns = self._invoke_fn(chunk)
        except self._lua_error as exc:  # 含 Hook 抛出的取消哨兵（Lua error）
            return RunResult(ok=False, values=(), error=str(exc))
        if not returns[1]:
            value = returns[2]
            return RunResult(False, (), str(value), value)
        return RunResult(True, tuple(returns[i] for i in range(2, returns["n"] + 1)), "")

    def make_host_binding(self, name: str, dispatch: Callable[..., Any]) -> Any:
        """构造宿主函数绑定（Lua 闭包 trampoline）。

        ``dispatch(*raw_args)`` 返回 ``HostOutcome``（鸭子类型：``ok`` / ``values`` /
        ``error``）；成功 → 转发值（可能多值），失败 → Lua 侧 ``error(error_table)``，
        使调用方 ``pcall`` 能捕获**错误表**（04）且**不泄漏 Python 对象**。
        """

        def _call(packed: Any) -> Any:
            count = packed["n"] or 0
            args = [packed[i] for i in range(1, count + 1)]
            outcome = dispatch(*args)
            if getattr(outcome, "ok", False):
                values = getattr(outcome, "values", ()) or ()
                value_table = self._rt.table()
                for index, item in enumerate(values, 1):
                    value_table[index] = item
                return self._rt.table(ok=True, values=value_table, n=len(values))
            return self._rt.table(ok=False, error=outcome.error)

        return self._make_binding_fn(name, _call)

    def install_hook(
        self,
        check: Callable[..., bool],
        *,
        mask: str = DEFAULT_HOOK_MASK,
        count: int = DEFAULT_COUNT_INTERVAL,
    ) -> None:
        hook = self._make_hook_fn(check, self.cancel_sentinel)
        self._sethook_fn(hook, mask, int(count))

    def clear_hook(self) -> None:
        self._sethook_fn()

    def memory_used(self) -> int:
        return int(self._rt.get_memory_used())

    def close(self) -> None:
        if self._rt is not None:
            try:
                self._sethook_fn()
            except Exception:  # 尽力清理；关闭不因 hook 清理失败而中断
                pass
            self._rt = None

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _import_lua54() -> Any:
        """导入 **Lua 5.4** 运行时（``lupa.lua54``）；缺失 → ``ENGINE_UNAVAILABLE``。"""
        try:
            import lupa.lua54 as lua54  # noqa: F401  (惰性：仅 LuaVm.build 路径触发)
        except ImportError as exc:
            raise engine_unavailable() from exc
        if getattr(lua54, "LuaRuntime", None) is None:
            raise engine_unavailable()
        return lua54


def _as_return_tuple(returns: Any) -> tuple:
    """把 lupa 的返回规整为元组：无返回（None）→ ``()``；单值 → ``(v,)``；多值 → 原元组。"""
    if returns is None:
        return ()
    if isinstance(returns, tuple):
        return returns
    return (returns,)


def monotonic_seconds() -> float:
    """默认单调时钟（保留给可注入场景；与 ``time.monotonic`` 同源）。"""
    return time.monotonic()
