# -*- coding: utf-8 -*-
"""取消令牌（TASK-007；对齐 01「所有长调用接收 CancelToken」）。

定义**最小**可注入接口，使长操作（捕获、位图解码）能协作式取消：

- ``CancelToken``：只要求 ``is_cancelled()`` 与 ``throw_if_cancelled()``；
- ``NullCancelToken``：永不取消（同步 IPC 命令层使用；无取消源时）；
- ``CancelFlag``：线程安全的手动取消位（供控制器/worker 共享）。

取消不是错误码（06）：取消以 ``CancelledError`` 异常表达，上层映射为
cancelled 状态而非 response.error。
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable


class CancelledError(Exception):
    """协作式取消信号（不是 06 错误码；上层映射为 cancelled 状态）。"""


@runtime_checkable
class CancelToken(Protocol):
    """长调用取消令牌最小接口。"""

    def is_cancelled(self) -> bool:
        """已请求取消返回 True。"""
        ...

    def throw_if_cancelled(self) -> None:
        """已取消则抛 ``CancelledError``，否则返回。"""
        ...


class NullCancelToken:
    """永不取消的令牌（无取消源场景；同步命令层缺省）。"""

    def is_cancelled(self) -> bool:
        return False

    def throw_if_cancelled(self) -> None:
        return None


class CancelFlag:
    """线程安全的手动取消位（可注入长操作）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        """请求取消（幂等）。"""
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancelledError("操作已取消")
