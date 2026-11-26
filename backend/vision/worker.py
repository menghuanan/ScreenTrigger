# -*- coding: utf-8 -*-
"""Coordinate recognition jobs and propagate per-call deadlines.

Each orchestration thread owns job state and waits on ProcessProvider. Native
recognition runs in the provider child, which is killed on cancellation or timeout.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional, Protocol, runtime_checkable

from .cancel import CancelToken, CancelledError, NullCancelToken

#: 单次推理 deadline（06/03：5s）。
INFERENCE_DEADLINE_MS: int = 5000


class DeadlineCancelToken:
    """协作式 deadline 令牌：``is_cancelled()`` = 基础取消 **或** 已过 deadline。"""

    def __init__(
        self,
        base: Optional[CancelToken] = None,
        *,
        deadline_ms: int = INFERENCE_DEADLINE_MS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if deadline_ms <= 0:
            raise ValueError("deadline_ms 必须为正")
        self._base: CancelToken = base if base is not None else NullCancelToken()
        self._deadline_s: float = deadline_ms / 1000.0
        self._monotonic = monotonic
        self._start: float = monotonic()
        self._timed_out: bool = False

    def timed_out(self) -> bool:
        """是否已越过 deadline（惰性求值；首次越线即固化）。"""
        if not self._timed_out and (self._monotonic() - self._start) >= self._deadline_s:
            self._timed_out = True
        return self._timed_out

    def is_cancelled(self) -> bool:
        # Preserve non-blocking polling for script gates, whose checkpoint can pause.
        poll = getattr(self._base, "is_cancelled", None)
        if poll is not None:
            return poll() or self.timed_out()
        # Earlier condition evaluators require only a throwing cancellation check.
        try:
            self._base.throw_if_cancelled()
        except CancelledError:
            return True
        return self.timed_out()

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancelledError("推理已超时或取消")


@runtime_checkable
class WorkerRunner(Protocol):
    """推理作业编排端口。"""

    def submit(self, run: Callable[[CancelToken], None], *, deadline_ms: int,
               cancel: Optional[CancelToken] = None) -> None:
        """提交一次推理（异步；``run`` 在 worker 线程执行）。"""
        ...

    def restart(self) -> None:
        """登记超时重启；子进程由持有该调用的 ProcessProvider 清理。"""
        ...

    def shutdown(self, timeout: Optional[float] = None) -> None:
        """关闭（尽力 join；供收尾调用）。"""
        ...


class ThreadInferenceWorker:
    """线程级 worker：每 job 独立守护线程 + ``DeadlineCancelToken``（协作式超时）。"""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._monotonic = monotonic
        self._logger = logger
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._restarts = 0

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def submit(
        self,
        run: Callable[[CancelToken], None],
        *,
        deadline_ms: int = INFERENCE_DEADLINE_MS,
        cancel: Optional[CancelToken] = None,
    ) -> None:
        """派生守护线程执行 ``run(deadline_token)``；立即返回（不阻塞调用方）。"""
        token = DeadlineCancelToken(cancel, deadline_ms=deadline_ms, monotonic=self._monotonic)

        def _target() -> None:
            try:
                run(token)
            except CancelledError:
                # 终态判定归 service 的 run 闭包；此处兜底不再上抛
                pass
            except Exception as exc:  # 防御：worker 线程绝不因未捕异常静默消失
                self._log("vision worker 未捕获异常：%r" % (exc,))

        thread = threading.Thread(target=_target, name="vision-worker", daemon=True)
        with self._lock:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)
            thread.start()

    def restart(self) -> None:
        """Record a timed-out orchestration; child ownership is per provider call."""
        with self._lock:
            self._restarts += 1
        self._log("vision job 超时，丢弃结果")

    def shutdown(self, timeout: Optional[float] = None) -> None:
        """尽力 join 已知 worker 线程（守护线程，不强杀）。"""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout)

    @property
    def restarts(self) -> int:
        with self._lock:
            return self._restarts

    @property
    def active_threads(self) -> int:
        with self._lock:
            return sum(1 for thread in self._threads if thread.is_alive())
