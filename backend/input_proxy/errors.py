# -*- coding: utf-8 -*-
"""输入窄代理错误映射（TASK-017 / M05）：host error code → DomainError。

调用方（TASK-021 的 Lua 输入 API）经代理拿到的失败一律是可读的
:class:`InputProxyError`，`code`/`message`/`details`/`retryable` 与 06 错误四字段
逐字一致。**retryable 只表示条件变化后允许调用方再试，代理不做任何自动重试**
（06：键鼠动作不自动重试）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# 06 错误码注册表（输入相关子集；代理不发明新码，未知码原样透传）。
INPUT_FAILED: str = "INPUT_FAILED"
REGION_LIMIT_VIOLATION: str = "REGION_LIMIT_VIOLATION"
STALE_FRAME: str = "STALE_FRAME"
WINDOW_NOT_FOUND: str = "WINDOW_NOT_FOUND"
WINDOW_UNAVAILABLE: str = "WINDOW_UNAVAILABLE"
BUSY: str = "BUSY"
INVALID_STATE: str = "INVALID_STATE"
CAPABILITY_DENIED: str = "CAPABILITY_DENIED"

# 输入代理会遇到的已知码集合（供调用方判断；不限制透传未知码）。
KNOWN_CODES = frozenset(
    {
        INPUT_FAILED,
        REGION_LIMIT_VIOLATION,
        STALE_FRAME,
        WINDOW_NOT_FOUND,
        WINDOW_UNAVAILABLE,
        BUSY,
        INVALID_STATE,
        CAPABILITY_DENIED,
        "INVALID_PARAMS",
        "UNKNOWN_COMMAND",
        "ENGINE_UNAVAILABLE",
        "INTERNAL_ERROR",
    }
)


class InputProxyError(Exception):
    """输入宿主调用的领域错误（DomainError）。

    Attributes:
        code: 06 错误码（字符串）。
        message: 面向用户的文案。
        details: 结构化细节（不含堆栈敏感对象）。
        retryable: 条件变化后是否允许调用方再试（不授权框架自动重跑动作）。
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Any] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.message: str = message
        self.details: Any = details if details is not None else {}
        self.retryable: bool = bool(retryable)

    def to_dict(self) -> Dict[str, Any]:
        """06 四字段字典。"""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return "InputProxyError(code=%r, retryable=%r)" % (self.code, self.retryable)


# 领域错误别名（设计 §3.2 命名 DomainError）。
DomainError = InputProxyError


def raise_for_host_error(error: Optional[Dict[str, Any]]) -> None:
    """把 host_response 的 error 对象（06 四字段）转成 :class:`InputProxyError` 抛出。

    缺字段按 INTERNAL_ERROR/空文案兜底；**不做重试、不做降级**。
    """
    if not error:
        raise InputProxyError("INTERNAL_ERROR", "宿主调用失败但未提供 error 对象")
    raise InputProxyError(
        code=str(error.get("code", "INTERNAL_ERROR")),
        message=str(error.get("message", "")),
        details=error.get("details"),
        retryable=bool(error.get("retryable", False)),
    )
