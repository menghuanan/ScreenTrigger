# -*- coding: utf-8 -*-
"""``backend.script`` barrel。

**TASK-012（既有）**：**仅编译**的脚本语法校验入口。

对外导出：

- ``ScriptValidator``：语法校验端口（可注入替身）；
- ``LupaScriptValidator``：基于 lupa（Lua 5.4）的仅编译实现；
- ``build_default_script_validator``：生产默认校验器；
- ``Diagnostic``：一条语法诊断（``line`` 1 起、``column`` 1 起或 null、``message``）；
- ``engine_unavailable`` / ``lupa_available`` / ``LUA_UNAVAILABLE_CODE``：
  lupa 缺失时的诚实降级（``ENGINE_UNAVAILABLE``、``retryable=False``、无 details）。

**TASK-020（本任务追加）**：Lua VM 运行容器入口（**不**执行编译期校验，运行时执行另属执行器）。

- ``errors``：06 错误构造器（语法/运行/序列化/深度）；
- ``cancel``：取消/暂停/期限闸门与可取消 wait（`CheckpointCancelled` 是控制信号，非错误码）；
- ``interop``：``LuaInterop`` 端口 + ``LupaInterop``（显式 Lua 5.4，缺失 → unavailable）；
- ``json_bridge``：Lua↔JSON 值桥（三态/哨兵/拒绝/深度/大小）；
- ``lua_vm``：``LuaVm``（环境白名单 / 私有 hooks / count 取消 / 内存限额）；
- ``hostapi``：``HostApiRegistry``（宿主函数登记、前后检查、值复制、异常归一）。

**不**触发任何实际执行（构建 VM 需显式调用 ``LuaVm.build()``）；lupa 为**执行路径依赖**
（缺 lupa 时 ``LuaVm.build()`` 诚实抛 ``ENGINE_UNAVAILABLE``）。
"""

from __future__ import annotations

# --- TASK-012 既有导出（保持键名不变） ---
from .validator import (
    LUA_UNAVAILABLE_CODE,
    LUA_UNAVAILABLE_MESSAGE,
    Diagnostic,
    LupaScriptValidator,
    ScriptValidator,
    build_default_script_validator,
    engine_unavailable,
    lupa_available,
)

# --- TASK-020 追加导出 ---
from .cancel import (
    CancelFlag,
    CancelToken,
    CheckpointCancelled,
    ExecutionGate,
    NullCancelToken,
    PauseFlag,
    cancellable_wait,
)
from .errors import (
    call_depth_exceeded,
    script_runtime_error,
    script_syntax_error,
    serialization_error,
)
from .hostapi import EnvNative, HostApiRegistry, HostOutcome
from .interop import (
    LuaChunkSyntaxError,
    LuaInterop,
    LupaInterop,
    RunResult,
)
from .json_bridge import (
    MAX_ENCODED_BYTES,
    MAX_JSON_DEPTH,
    MAX_SAFE_INT,
    JsonBridge,
    Sentinels,
    make_sentinels,
    measure_encoded_size,
)
from .lua_vm import (
    API_VERSION,
    BLOCKED_NAMES,
    COUNT_HOOK_INTERVAL,
    DEFAULT_MEMORY_LIMIT_MB,
    CallStackTracker,
    LuaVm,
)

__all__ = [
    # TASK-012
    "LUA_UNAVAILABLE_CODE",
    "LUA_UNAVAILABLE_MESSAGE",
    "Diagnostic",
    "LupaScriptValidator",
    "ScriptValidator",
    "build_default_script_validator",
    "engine_unavailable",
    "lupa_available",
    # TASK-020：errors
    "serialization_error",
    "call_depth_exceeded",
    "script_syntax_error",
    "script_runtime_error",
    # TASK-020：cancel
    "CheckpointCancelled",
    "CancelToken",
    "NullCancelToken",
    "CancelFlag",
    "PauseFlag",
    "ExecutionGate",
    "cancellable_wait",
    # TASK-020：interop
    "LuaInterop",
    "LupaInterop",
    "LuaChunkSyntaxError",
    "RunResult",
    # TASK-020：json_bridge
    "JsonBridge",
    "Sentinels",
    "make_sentinels",
    "measure_encoded_size",
    "MAX_JSON_DEPTH",
    "MAX_ENCODED_BYTES",
    "MAX_SAFE_INT",
    # TASK-020：lua_vm
    "LuaVm",
    "CallStackTracker",
    "COUNT_HOOK_INTERVAL",
    "DEFAULT_MEMORY_LIMIT_MB",
    "API_VERSION",
    "BLOCKED_NAMES",
    # TASK-020：hostapi
    "HostApiRegistry",
    "HostOutcome",
    "EnvNative",
]
