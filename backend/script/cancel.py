# -*- coding: utf-8 -*-
"""Lua 执行取消 / 暂停 / 期限原语（TASK-020 §4.2 cancel.py）。

依据：

- ``docs/06`` §时间、取消与重试：stop 先关闭闸门，再通知 Python；Lua count hook、
  API 前后检查及可取消 wait **共同**确保忙循环可停；取消是**控制信号不是错误码**。
- ``docs/04``：取消/停止是控制信号，即使 ``pcall`` 截获内部异常，执行闸门仍关闭且 hook
  会继续退出，不可借此继续输入。
- ``docs/07`` §3：宿主函数装饰器每次调用前后检查取消/暂停/根期限；wait 使用最多 50ms
  切片，不 sleep 整段时长。

范式与 ``backend.vision.cancel`` / ``backend.vision.worker`` **同构**（协作式取消令牌 +
可注入单调时钟），但**不 import backend.vision**（守住 ``Script`` 模块边界，取消原语本地
同构，见设计 §2.2）。

``CheckpointCancelled`` 是**控制信号**，**不是** 06 错误码；终态映射（cancelled/stopping）
属 TASK-022（见设计 §6 Q9）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# 控制信号
# ---------------------------------------------------------------------------


class CheckpointCancelled(Exception):
    """协作式取消控制信号（**非** 06 错误码；上层映射 cancelled 状态）。

    由执行闸门（``ExecutionGate.checkpoint``）、count/line hook 与宿主函数前后检查抛出。
    04：取消不是错误码；pcall 捕获一次后闸门保持关闭，下一次 tick 仍会再次触发。
    """

    def __init__(self, message: str = "执行已被取消或超出期限") -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# 取消令牌 / 暂停位
# ---------------------------------------------------------------------------


@runtime_checkable
class CancelToken(Protocol):
    """长调用取消令牌最小接口（与 ``backend.vision.cancel.CancelToken`` 结构等价）。"""

    def is_cancelled(self) -> bool:
        """已请求取消返回 True。"""
        ...

    def throw_if_cancelled(self) -> None:
        """已取消则抛 ``CheckpointCancelled``，否则返回。"""
        ...


class NullCancelToken:
    """永不取消的令牌（无取消源场景）。"""

    def is_cancelled(self) -> bool:
        return False

    def throw_if_cancelled(self) -> None:
        return None


class CancelFlag:
    """线程安全的手动取消位（超时/外部取消共享；幂等）。"""

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
            raise CheckpointCancelled("操作已取消")


class PauseFlag:
    """暂停位（``pause``/``resume``/``is_paused``；线程安全）。

    暂停**编排**（阻塞在 Hook / 进入 paused 状态）归 TASK-022；本任务只提供可判定的位与
    ``ExecutionGate.is_paused``（设计 §4.2 / §6 Q9）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = False

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused


# ---------------------------------------------------------------------------
# 执行闸门
# ---------------------------------------------------------------------------


@dataclass
class ExecutionGate:
    """执行闸门：合成 取消令牌 / 暂停位 / 根期限（可注入时钟）。

    Attributes:
        cancel: 取消令牌（默认 ``NullCancelToken``）。
        pause: 暂停位（默认未暂停）。
        deadline_ms: 相对启动的根期限（毫秒）；``None`` 表示无期限。
        monotonic: 单调时钟（可注入，便于确定性测试）。
    """

    cancel: CancelToken = field(default_factory=NullCancelToken)
    pause: PauseFlag = field(default_factory=PauseFlag)
    deadline_ms: Optional[int] = None
    monotonic: Callable[[], float] = time.monotonic
    _start: Optional[float] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.deadline_ms is not None and self.deadline_ms < 0:
            raise ValueError("deadline_ms 不得为负")

    # -- 内部 ----------------------------------------------------------------

    def _start_time(self) -> float:
        """首次访问时固化启动时刻（惰性；不因构造与首次检查之间的间隔误判）。"""
        if self._start is None:
            self._start = self.monotonic()
        return self._start

    # -- 查询 ----------------------------------------------------------------

    def deadline_exceeded(self) -> bool:
        """是否已越过根期限（相对启动的毫秒）。"""
        if self.deadline_ms is None:
            return False
        elapsed_ms = (self.monotonic() - self._start_time()) * 1000.0
        return elapsed_ms >= self.deadline_ms

    def is_cancelled(self) -> bool:
        """已取消 **或** 已超期。"""
        return self.cancel.is_cancelled() or self.deadline_exceeded()

    def is_paused(self) -> bool:
        return self.pause.is_paused()

    def remaining_ms(self) -> Optional[int]:
        """剩余期限毫秒；无期限返回 ``None``，已到期返回 0。"""
        if self.deadline_ms is None:
            return None
        remaining = self.deadline_ms - (self.monotonic() - self._start_time()) * 1000.0
        return int(remaining) if remaining > 0 else 0

    def checkpoint(self) -> None:
        """取消 / 超期 → 抛 ``CheckpointCancelled``（04/06：取消是控制信号）。"""
        if self.is_cancelled():
            raise CheckpointCancelled("执行已被取消或超出期限")


# ---------------------------------------------------------------------------
# 可取消等待
# ---------------------------------------------------------------------------


def cancellable_wait(
    seconds: float,
    gate: ExecutionGate,
    *,
    slice_ms: int = 50,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """可取消等待（≤ ``slice_ms`` 切片；**不 sleep 整段时长**）。

    Args:
        seconds: 等待秒数（``>=0``）。
        gate: 执行闸门（每片检查取消/期限）。
        slice_ms: 单次睡眠上限（毫秒，默认 50，见 07§3）。
        monotonic: 单调时钟（可注入）。
        sleep: 睡眠函数（可注入）。

    Returns:
        True 表示等待完整结束；False 表示期间被取消（不 sleep 整段）。
    """
    if seconds < 0:
        raise ValueError("seconds 不得为负")
    if slice_ms <= 0:
        raise ValueError("slice_ms 必须为正")
    slice_s = slice_ms / 1000.0
    deadline = monotonic() + float(seconds)
    while True:
        if gate.is_cancelled():
            return False
        now = monotonic()
        if now >= deadline:
            return True
        sleep(min(slice_s, deadline - now))
