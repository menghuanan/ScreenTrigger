# -*- coding: utf-8 -*-
"""宿主函数注册机制（TASK-020 §4.2 hostapi.py）。

职责：

- ``HostApiRegistry``：登记宿主函数（TASK-021 填充业务 API；本任务仅登记
  ``json_array`` / ``json_null``）；
- **每次调用前后** ``gate.checkpoint()``（07§3：检查取消/暂停/根期限）；
- 参数与返回值**值复制**（经 ``JsonBridge``；绝不共享 table / 泄漏 Python 对象）；
- 异常归一为 Lua **错误表** ``{code,message,details}``（04；**无** Python 回溯）；
- 取消是**控制信号**：以宿主私有哨兵（非 06 错误码）经 Lua ``error`` 抛出；闸门保持关闭，
  ``pcall`` 捕获一次后下一次 tick / 下一次宿主调用仍会再抛（04：pcall 不可绕过闸门）。

**值语义**：声明为普通宿主函数（``raw=False``）时，入参先经 ``bridge.lua_to_json`` 复制为
JSON 值再交给业务函数；返回值经 ``bridge.json_to_lua`` 生成全新 Lua 值。声明为 ``raw=True``
的宿主函数（如 ``json_array``）接收/返回**环境原生**值，用于构建带标记的结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from ..storage.errors import StorageError
from .cancel import CheckpointCancelled, ExecutionGate
from .json_bridge import JsonBridge, Sentinels


class EnvNative:
    """标记：宿主函数返回的**环境原生**值，绕过 ``json_to_lua`` 直接转发。

    仅用于必须构建原生结构（如带私有 metatable 的数组标记表）的宿主函数。
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


@dataclass
class HostOutcome:
    """宿主函数一次调用的结果信封（不含任何 Python 异常对象）。"""

    ok: bool
    values: Tuple[Any, ...] = ()
    error: Any = None  # 失败时为环境原生错误表（{code,message,details}）或取消哨兵


class HostApiRegistry:
    """宿主函数登记器 + 调用包装器（前后检查、值复制、异常归一）。"""

    def __init__(
        self,
        gate: ExecutionGate,
        bridge: JsonBridge,
        sentinels: Optional[Sentinels] = None,
        interop: Any = None,
    ) -> None:
        self._gate = gate
        self._bridge = bridge
        self._sentinels: Sentinels = sentinels if sentinels is not None else bridge.sentinels
        self._interop = interop if interop is not None else bridge.interop
        #: name -> (fn, raw?)
        self._functions: Dict[str, Tuple[Callable[..., Any], bool]] = {}
        #: name -> 环境原生值（如 json_null 哨兵）
        self._literals: Dict[str, Any] = {}

    # -- 注册 ----------------------------------------------------------------

    def register(self, name: str, fn: Callable[..., Any], *, raw: bool = False) -> None:
        """登记宿主函数（``fn`` 接收 JSON 值；``raw=True`` 时接收环境原生值）。"""
        if not isinstance(name, str) or not name:
            raise ValueError("宿主函数名必须为非空字符串")
        if not callable(fn):
            raise ValueError("宿主函数必须可调用")
        self._functions[name] = (fn, bool(raw))

    def install_literal(self, name: str, value: Any) -> None:
        """登记环境原生字面量（注入用户 env 的值，如 ``json_null``）。"""
        if not isinstance(name, str) or not name:
            raise ValueError("字面量名必须为非空字符串")
        self._literals[name] = value

    def install_json_markers(self) -> None:
        """登记 ``json_null``（哨兵字面量）与 ``json_array``（数组标记函数，04§控制与工具）。"""
        self.install_literal("json_null", self._sentinels.null)
        self.register("json_array", self._json_array, raw=True)

    # -- 包装 ----------------------------------------------------------------

    def wrap(self, fn: Callable[..., Any], *, raw: bool = False) -> Callable[..., HostOutcome]:
        """返回一个**从不抛异常**的调度器：``dispatch(*raw_args) -> HostOutcome``。

        与设计 §4.2 的一致性：本调度器即 ``wrap(fn)`` 的产物，供 ``LuaInterop.make_host_binding``
        生成环境原生绑定（真实环境为 Lua 闭包 trampoline）。返回信封（而非直接值/异常）以
        保证**错误以 Lua 错误表**形式由 Lua 侧 ``error`` 抛出，且绝不泄漏 Python 对象。
        """

        def dispatch(*raw_args: Any) -> HostOutcome:
            return self._dispatch(fn, raw_args, raw)

        return dispatch

    def bindings(self) -> Dict[str, Callable[..., HostOutcome]]:
        """名称 → 调度器（供 VM 经 ``LuaInterop.make_host_binding`` 绑定到用户 env）。"""
        return {
            name: self.wrap(fn, raw=raw)
            for name, (fn, raw) in self._functions.items()
        }

    def literals(self) -> Dict[str, Any]:
        """名称 → 环境原生字面量（供 VM 注入用户 env）。"""
        return dict(self._literals)

    # -- 内部：调度 ----------------------------------------------------------

    def _dispatch(
        self, fn: Callable[..., Any], raw_args: Tuple[Any, ...], raw: bool
    ) -> HostOutcome:
        """调用前检查 → 值复制 → 调用 → 调用后检查 → 值复制返回；异常归一。"""
        # 1) 调用前检查（取消/期限）
        try:
            self._gate.checkpoint()
        except CheckpointCancelled:
            return self._cancelled()
        # 2) 入参复制
        if raw:
            json_args: Tuple[Any, ...] = tuple(raw_args)
        else:
            try:
                json_args = tuple(
                    self._bridge.lua_to_json(arg, context="argument") for arg in raw_args
                )
            except StorageError as exc:
                return self._error(exc)
        # 3) 业务调用
        try:
            result = fn(*json_args)
        except CheckpointCancelled:
            return self._cancelled()
        except StorageError as exc:
            return self._error(exc)
        except TypeError as exc:
            return self._error(StorageError("INVALID_PARAMS", str(exc)))
        except Exception as exc:
            if isinstance(getattr(exc, "code", None), str):
                return self._error(StorageError(exc.code, str(exc), field=getattr(exc, "field", None),
                                                details=getattr(exc, "details", {})))
            return self._error(StorageError("INTERNAL_ERROR", str(exc)))
        # 4) 调用后检查
        try:
            self._gate.checkpoint()
        except CheckpointCancelled:
            return self._cancelled()
        # 5) 返回值复制
        return self._marshal(result, raw)

    def _marshal(self, result: Any, raw: bool) -> HostOutcome:
        """把业务返回值规整为信封（``EnvNative`` 直通；``raw`` 结果直通）。"""
        values = result if isinstance(result, tuple) else (result,)
        out = []
        for value in values:
            if isinstance(value, EnvNative):
                out.append(value.value)
            elif raw:
                out.append(value)
            else:
                try:
                    out.append(self._bridge.json_to_lua(value))
                except StorageError as exc:
                    return self._error(exc)
        return HostOutcome(ok=True, values=tuple(out))

    def _error(self, error: StorageError) -> HostOutcome:
        """``StorageError`` → 环境原生 Lua 错误表 ``{code,message,details}``（04；无回溯）。"""
        table = {
            "code": error.code,
            "message": error.message,
            "details": dict(error.details) if error.details else {},
        }
        return HostOutcome(ok=False, error=self._bridge.json_to_lua(table))

    def _cancelled(self) -> HostOutcome:
        """取消控制信号 → 宿主私有哨兵（**非** 06 错误码；闸门保持关闭）。"""
        return HostOutcome(ok=False, error=self._cancel_marker())

    def _cancel_marker(self) -> Any:
        marker = getattr(self._interop, "cancel_sentinel", None)
        if marker is None:
            # 无哨兵能力时的兜底（仍为宿主私有表，不泄漏 Python 对象）
            return self._bridge.json_to_lua({"__st_control": "checkpoint_cancelled"})
        return marker

    # -- 内置宿主函数（json_array） ------------------------------------------

    def _json_array(self, table: Any) -> EnvNative:
        """``json_array(t)``：连续 1..n 的 table → 数组标记（04；空表合法）。

        非法入参（非 table / 非连续 1..n）→ ``INVALID_PARAMS``（设计 §6 Q5）。
        """
        interop = self._interop
        if interop.lua_type(table) != "table":
            raise StorageError("INVALID_PARAMS", "json_array 需要 table 入参", field="table")
        keys = list(interop.table_keys(table))
        indices = []
        for key in keys:
            if (
                interop.lua_type(key) != "number"
                or isinstance(key, bool)
                or not isinstance(key, int)
            ):
                raise StorageError(
                    "INVALID_PARAMS", "json_array 需要连续整数键 1..n", field="table"
                )
            indices.append(int(key))
        count = len(indices)
        if set(indices) != set(range(1, count + 1)):
            raise StorageError(
                "INVALID_PARAMS", "json_array 需要连续整数键 1..n", field="table"
            )
        marked = interop.new_table()
        for index in range(1, count + 1):
            interop.table_set(marked, index, interop.table_get(table, index))
        interop.setmetatable(marked, self._sentinels.array_mt)
        return EnvNative(marked)
