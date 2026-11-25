# -*- coding: utf-8 -*-
"""Lua 5.4 独立 VM：环境白名单 / 私有 hooks / 取消闸门（TASK-020 §4.2 lua_vm.py）。

依据 ``docs/07`` §3（逐字遵守）与 ``docs/04`` §环境：

- **显式** ``lupa.lua54`` 并读 ``_VERSION`` 复核为 ``"Lua 5.4"``；缺失/不符 → ``ENGINE_UNAVAILABLE``；
- 用户 chunk 绑定**全新** ``_ENV``（``setmetatable({}, {__index = whitelist})``），库白名单
  base/string/table/math/utf8 **子集**；用户 env **无** io/os/package/require/load/loadfile/
  dofile/debug、**无** coroutine、**无** getmetatable/setmetatable（设计 §6 Q4 裁决：收紧）；
- 宿主**预保留** ``debug.sethook/getinfo/getlocal`` 私有引用（宿主私有，不入用户 env）；
- ``line``（断点基础）、``count``（周期检查取消，间隔从 10000 指令评估）、``call``/``return``
  （维护调用深度）hook；
- 取消不变量：count/line hook **每次触发**都检查闸门并在关闭时经 Lua ``error`` 抛私有哨兵
  （`pcall` 吞掉一次后下一次 tick 仍再抛）；配合 API 前后检查与可取消 wait，忙循环可停；
- 内存限额**按 VM 参数化**（建议 128MiB/根树）；根树多 VM 合计策略属 TASK-022（``child_vm``
  预留）。

**不 import backend.vision**；**不改** ``validator.py``（只复用其 ``engine_unavailable``）。
"""

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional, Tuple

from .cancel import CheckpointCancelled, ExecutionGate
from .errors import script_runtime_error, script_syntax_error
from ..storage.errors import StorageError
from .hostapi import HostApiRegistry
from .interop import LuaChunkSyntaxError, LuaInterop, LupaInterop
from .json_bridge import JsonBridge, make_sentinels

#: count hook 间隔（07§3：从 10000 指令评估，**不承诺固定耗时**）。
COUNT_HOOK_INTERVAL: int = 10000
#: 每 VM Lua 堆内存限额（06：每根执行树 Lua 堆合计建议 128MiB）。
DEFAULT_MEMORY_LIMIT_MB: int = 128
#: Lua API 版本（04：``API_VERSION="1"``）。
API_VERSION: str = "1"

#: base 白名单子集（04；**不含** getmetatable/setmetatable，Q4 收紧；保留 print 待 TASK-021 重定向）。
WHITELIST_BASE: Tuple[str, ...] = (
    "assert", "error", "ipairs", "next", "pairs", "pcall", "xpcall", "select",
    "tostring", "tonumber", "type", "rawget", "rawset", "rawequal", "rawlen", "print",
)
#: string 白名单子集。
WHITELIST_STRING: Tuple[str, ...] = (
    "byte", "char", "find", "format", "gmatch", "gsub", "len", "lower", "match",
    "rep", "reverse", "sub", "upper",
)
#: table 白名单子集。
WHITELIST_TABLE: Tuple[str, ...] = (
    "concat", "insert", "move", "pack", "remove", "sort", "unpack",
)
#: math 白名单子集。
WHITELIST_MATH: Tuple[str, ...] = (
    "abs", "ceil", "floor", "huge", "max", "min", "random", "randomseed", "sqrt", "pi",
)
#: utf8 白名单子集。
WHITELIST_UTF8: Tuple[str, ...] = (
    "char", "charpattern", "codepoint", "codes", "len", "offset",
)
#: 用户 env **必须不可达** 的名字（04 + Q4 收紧）。
BLOCKED_NAMES: Tuple[str, ...] = (
    "io", "os", "package", "require", "load", "loadfile", "dofile", "debug",
    "coroutine", "collectgarbage", "_G", "getmetatable", "setmetatable",
)

_CHUNKNAME_SANITIZE_RE = re.compile(r"[^0-9A-Za-z_-]")
_ERROR_RE = re.compile(r"^.*?:(\d+):\s*(.*)$", re.S)
_NEAR_RE = re.compile(r"near '([^']*)'")


def _build_whitelist_lua() -> str:
    """生成构造白名单 table 的 Lua 片段（宿主私有；用户 env 经 __index 引用）。"""
    base = ", ".join(f"{name} = {name}" for name in WHITELIST_BASE if name != "print")
    return (
        "(function() local wl = {\n"
        "  " + base + ",\n"
        "  string = {}, table = {}, math = {}, utf8 = {},\n"
        "}\n"
        "local function copy(src, dst, names)\n"
        "  for _, n in ipairs(names) do dst[n] = src[n] end\n"
        "end\n"
        "copy(string, wl.string, { %s })\n"
        "copy(table, wl.table, { %s })\n"
        "copy(math, wl.math, { %s })\n"
        "copy(utf8, wl.utf8, { %s })\n"
        "return wl end)()\n"
    ) % (
        ", ".join('"%s"' % name for name in WHITELIST_STRING),
        ", ".join('"%s"' % name for name in WHITELIST_TABLE),
        ", ".join('"%s"' % name for name in WHITELIST_MATH),
        ", ".join('"%s"' % name for name in WHITELIST_UTF8),
    )


class CallStackTracker:
    """Lua 调用栈深度计数（call/return hook 维护；**深度 5 强制策略属 TASK-022**）。"""

    def __init__(self, max_depth: int = 5) -> None:
        self.depth: int = 0
        self.max_depth: int = int(max_depth)

    def on_call(self) -> None:
        self.depth += 1

    def on_return(self) -> None:
        if self.depth > 0:
            self.depth -= 1

    def reset(self) -> None:
        self.depth = 0


class LuaVm:
    """Lua 5.4 独立 VM（环境 / hooks / 取消 / 加载 / 运行 / 内存限额）。"""

    def __init__(
        self,
        gate: ExecutionGate,
        *,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        count_hook_interval: int = COUNT_HOOK_INTERVAL,
        registry: Optional[HostApiRegistry] = None,
        interop: Optional[LuaInterop] = None,
    ) -> None:
        self.gate = gate
        self.memory_limit_mb = int(memory_limit_mb)
        self.count_hook_interval = int(count_hook_interval)
        self._injected_interop = interop
        self._injected_registry = registry
        self.interop: Optional[LuaInterop] = None
        self.bridge: Optional[JsonBridge] = None
        self.registry: Optional[HostApiRegistry] = None
        self.tracker = CallStackTracker()
        self._sentinels = None
        self._whitelist: Any = None
        self._env: Any = None
        self._on_line: Optional[Callable[[int], None]] = None
        self._hooks_installed = False
        self._closed = False

    # -- 生命周期 ------------------------------------------------------------

    def build(self) -> None:
        """构建 VM：``interop.build()`` → 白名单 → 哨兵/桥 → 注册器。

        Raises:
            StorageError: ``ENGINE_UNAVAILABLE``（无 lupa / 非 Lua 5.4）。
        """
        self.interop = self._injected_interop or LupaInterop(max_memory_mb=self.memory_limit_mb)
        self.interop.build()  # 缺 lupa / 版本不符 → ENGINE_UNAVAILABLE（诚实降级）
        self._whitelist = self.interop.eval(_build_whitelist_lua())
        self._sentinels = make_sentinels(self.interop)
        self.bridge = JsonBridge(self.interop, self._sentinels)
        if self._injected_registry is None:
            self.registry = HostApiRegistry(
                self.gate, self.bridge, self._sentinels, self.interop
            )
        else:
            self.registry = self._injected_registry
        self.registry.install_json_markers()
        self.install_hooks()

    def load(
        self,
        source: str,
        name: str,
        *,
        script_id: str = "",
        execution_id: str = "",
    ) -> Any:
        """构造受限 ``_ENV``、注入全局、``load`` 用户 chunk（仅编译，不执行）。

        Raises:
            StorageError: ``SCRIPT_SYNTAX_ERROR``（编译失败，含行列诊断）。
        """
        if self.interop is None:
            raise RuntimeError("VM 未构建（先调用 build()）")
        env = self.interop.new_env(self._whitelist)
        self.interop.table_set(env, "SCRIPT_ID", script_id)
        self.interop.table_set(env, "EXECUTION_ID", execution_id)
        self.interop.table_set(env, "API_VERSION", API_VERSION)
        self.interop.table_set(env, "args", self.interop.new_table())
        # 字面量（json_null 哨兵）与宿主函数绑定
        for literal_name, literal_value in self.registry.literals().items():
            self.interop.table_set(env, literal_name, literal_value)
        for fn_name, dispatch in self.registry.bindings().items():
            binding = self.interop.make_host_binding(fn_name, dispatch)
            self.interop.table_set(env, fn_name, binding)
        chunkname = "@" + _sanitize_chunkname(name)
        try:
            chunk = self.interop.load_chunk(source, chunkname, env)
        except LuaChunkSyntaxError as exc:
            line, column, message = _parse_lua_error(exc.message, source)
            raise script_syntax_error(message, line=line, column=column) from exc
        self._env = env
        return chunk

    def install_hooks(self, *, on_line: Optional[Callable[[int], None]] = None) -> None:
        """安装 line/count/call/return hook（经宿主私有 ``sethook``）。"""
        self._on_line = on_line
        if self._hooks_installed or self.interop is None:
            return
        self.interop.install_hook(
            self._make_hook_check(),
            mask="crl",
            count=self.count_hook_interval,
        )
        self._hooks_installed = True

    def run(self, chunk: Any, args: Optional[dict] = None) -> List[Any]:
        """注入 ``args``（值复制）并执行 chunk，返回**原始** Lua 返回值列表。

        - 执行前/后检查闸门；取消/超期 → 抛 ``CheckpointCancelled``（控制信号，**非** 06 码）；
        - Lua 未处理错误 → ``SCRIPT_RUNTIME_ERROR``；若闸门此时关闭则归为取消；
        - 桥接（``bridge.bridge_return``）由调用方完成（设计 §4.2）。
        """
        if self.interop is None:
            raise RuntimeError("VM 未构建（先调用 build()）")
        if self._env is None:
            raise RuntimeError("未加载 chunk（先调用 load()）")
        self.interop.table_set(
            self._env, "args", self.bridge.json_to_lua(dict(args) if args else {})
        )
        self.tracker.reset()
        self.gate.checkpoint()  # API 前检查
        result = self.interop.invoke(chunk)
        if self.gate.is_cancelled():  # 取消是控制信号（含 pcall 吞掉一次的情形）
            raise CheckpointCancelled("执行已被取消或超出期限")
        if not result.ok:
            value = getattr(result, "error_value", None)
            if value is not None and self.interop.lua_type(value) == "table":
                data = self.bridge.lua_to_json(value, context="return")
                if isinstance(data, dict) and isinstance(data.get("code"), str):
                    raise StorageError(data["code"], str(data.get("message", "")),
                                       details=data.get("details") or {})
            raise script_runtime_error(result.error or "Lua 运行错误")
        return list(result.values)

    def run_source(
        self,
        source: str,
        name: str,
        args: Optional[dict] = None,
        *,
        script_id: str = "",
        execution_id: str = "",
        on_line: Optional[Callable[[int], None]] = None,
    ) -> Any:
        """便捷：``load`` + ``run`` + ``bridge_return`` → 返回 JSON 值（供集成/契约测试）。"""
        if on_line is not None:
            self.install_hooks(on_line=on_line)
        chunk = self.load(source, name, script_id=script_id, execution_id=execution_id)
        values = self.run(chunk, args)
        return self.bridge.bridge_return(values)

    def depth(self) -> int:
        """当前 Lua 调用深度（预留：TASK-022 深度 5 强制）。"""
        return self.tracker.depth

    def child_vm(self) -> "LuaVm":
        """预留：TASK-022 ``call_script``（共享闸门、独立 VM/堆）。**本任务不实现策略**。"""
        return LuaVm(
            self.gate,
            memory_limit_mb=self.memory_limit_mb,
            count_hook_interval=self.count_hook_interval,
        )

    def memory_used(self) -> int:
        """当前 VM Lua 堆字节用量（口径：每 VM Lua 堆，非进程 RSS）。"""
        if self.interop is None:
            raise RuntimeError("VM 未构建（先调用 build()）")
        return self.interop.memory_used()

    def close(self) -> None:
        """关闭：清 hook、释放运行时引用（幂等）。"""
        if self.interop is not None and not self._closed:
            try:
                self.interop.clear_hook()
            except Exception:
                pass
            self.interop.close()
        self._closed = True
        self._hooks_installed = False
        self._env = None
        self._whitelist = None
        self.registry = None
        self.bridge = None
        self._sentinels = None

    # -- 内部 ----------------------------------------------------------------

    def _make_hook_check(self) -> Callable[..., bool]:
        """宿主 hook 回调：更新调用深度 / 触发 line 回调 / 返回是否需要取消。"""

        def check(event: Any, line: Any = None) -> bool:
            name = str(event)
            if name == "call":
                self.tracker.on_call()
            elif name == "return":
                self.tracker.on_return()
            elif name == "line" and self._on_line is not None:
                try:
                    self._on_line(int(line) if line is not None else 0)
                except CheckpointCancelled:
                    raise
                except Exception:
                    # 调试回调异常不得破坏执行；取消判定仍由闸门决定
                    pass
            if self.gate.is_paused():
                try:
                    self.gate.checkpoint()
                except CheckpointCancelled:
                    return True
            return self.gate.is_cancelled()

        return check


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _sanitize_chunkname(name: str) -> str:
    """净化 chunkname（去冒号/换行/非 ASCII）→ 使 Lua 错误串可稳定解析。"""
    safe = _CHUNKNAME_SANITIZE_RE.sub("_", name or "")
    safe = safe.strip("_")[:48] or "script"
    return safe


def _parse_lua_error(errmsg: str, source: str) -> Tuple[int, Optional[int], str]:
    """从 Lua 错误串解析 ``(line, column, message)``；列号仅在能无歧义定位时给出。"""
    match = _ERROR_RE.match(errmsg.strip())
    if match is None:
        return 1, None, errmsg.strip()
    line = int(match.group(1))
    message = match.group(2).strip()
    column: Optional[int] = None
    near = _NEAR_RE.search(message)
    if near is not None and near.group(1) != "":
        token = near.group(1)
        lines = source.split("\n")
        if 1 <= line <= len(lines) and lines[line - 1].count(token) == 1:
            column = lines[line - 1].index(token) + 1
    return line, column, message
