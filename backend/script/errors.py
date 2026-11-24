# -*- coding: utf-8 -*-
"""Lua VM 错误构造器（TASK-020 §4.2 errors.py）。

依据 ``docs/06`` §错误码注册表（四字段 ``{code,message,details,retryable}``）：

- ``SCRIPT_SYNTAX_ERROR``：Lua 编译错误（修正行列，retryable=false）；
- ``SCRIPT_RUNTIME_ERROR``：Lua 未处理错误（调试调用栈，retryable=false）；
- ``SERIALIZATION_ERROR``：非 JSON 参数/返回（修正数据，retryable=false）；
- ``CALL_DEPTH_EXCEEDED``：**子调用第 6 层**（改脚本结构，retryable=false）；
- ``ENGINE_UNAVAILABLE``：进程/引擎不可用（**复用** ``validator.engine_unavailable()``，
  单一事实源；retryable=true、无 details）。

**归因裁决（设计 §6 Q2，主理人已采纳）**：JSON **值**问题（含嵌套深度 >32、编码 >1MiB、
混合键、稀疏数组、循环 table、函数、userdata、NaN/Inf、整数越界）统一抛
``SERIALIZATION_ERROR``；``CALL_DEPTH_EXCEEDED`` **只**用于 **Lua 子调用栈深度**
（06 注册表定义为「子调用第 6 层」），由 TASK-022 的 ``call_script`` 在第 6 层抛出。
本任务的 ``call_depth_exceeded`` 构造器为 TASK-022 **预留**，JSON 桥**不**使用它——
用 ``CALL_DEPTH_EXCEEDED`` 表达 JSON 嵌套属误归因。

本模块**只构造** ``StorageError``（复用 ``backend.storage.errors.StorageError`` 四字段），
不改任何既有行为。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..storage.errors import StorageError

# ---------------------------------------------------------------------------
# 具名构造器（code 与 retryable 集中一处，避免漂移）
# ---------------------------------------------------------------------------


def serialization_error(
    message: str,
    *,
    field: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> StorageError:
    """``SERIALIZATION_ERROR``（06：非 JSON 参数/返回；retryable=False）。

    Args:
        message: 人类可读描述。
        field: ``details.field`` 定位（如 ``return`` / ``argument`` / ``value``）。
        details: 附加上下文（如实际字节数）。
    """
    return StorageError("SERIALIZATION_ERROR", message, field=field, details=details)


def call_depth_exceeded(message: str, *, depth: int, limit: int) -> StorageError:
    """``CALL_DEPTH_EXCEEDED``（06：子调用第 6 层；retryable=False）。

    **预留**给 TASK-022 的 ``call_script`` 子调用栈深度；JSON 桥**不**使用本构造器
    （见模块 docstring 的 Q2 归因裁决）。
    """
    return StorageError(
        "CALL_DEPTH_EXCEEDED",
        message,
        details={"depth": int(depth), "limit": int(limit)},
    )


def script_syntax_error(
    message: str, *, line: int, column: Optional[int]
) -> StorageError:
    """``SCRIPT_SYNTAX_ERROR``（06：Lua 编译错误；retryable=False）。

    ``line`` 为 1 起行号；``column`` 只在能**无歧义**推导时给出，否则 ``None``
    （对齐 02「引擎无法判断时为 null，不编造」）。
    """
    return StorageError(
        "SCRIPT_SYNTAX_ERROR",
        message,
        details={"line": int(line), "column": column},
    )


def script_runtime_error(message: str) -> StorageError:
    """``SCRIPT_RUNTIME_ERROR``（06：Lua 未处理错误；retryable=False）。"""
    return StorageError("SCRIPT_RUNTIME_ERROR", message)


# 说明：ENGINE_UNAVAILABLE 复用 ``backend.script.validator.engine_unavailable()``
# （设计 §4.2：单一事实源），此处**不**重新实现，避免口径漂移。
