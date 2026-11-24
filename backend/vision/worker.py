# -*- coding: utf-8 -*-
"""推理 worker 端口与线程实现（TASK-013 §7.2；超时/取消落点）。

设计：
- ``WorkerRunner`` 端口正式命名（线程实现 + **进程实现预留**，Q2）；TASK-014/015 视原生
  可中断性决定是否引入进程级实现；
- ``ThreadInferenceWorker``：每个 job 一个**独立守护线程**执行 ``run``；**不阻塞** IPC reader
  与输入闸门（命令 handler 只创建 job 即刻返回）；
- ``DeadlineCancelToken``：基于**单调时钟**的 5s deadline（注入 ``monotonic`` 便于确定性测试），
  与基础取消令牌（``JobBoundCancelToken``）合成；provider 的协作式取消点调用
  ``cancel.throw_if_cancelled()`` 即感知超时；provider 返回/抛错后 runner 检查 ``timed_out()``
  判定 ``RECOGNITION_TIMEOUT``（超时**丢弃结果**）；
- **不可中断原生调用**（线程级实现局限）：`restart()` 为 **abandon 语义**（丢弃该 job 执行上下文，
  守护线程泄漏至原生调用返回；**不阻塞**后续 job）。

**不改** ``backend/ipc/jobs.py``/``dispatcher.py``。
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
        return self._base.is_cancelled() or self.timed_out()

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancelledError("推理已超时或取消")


@runtime_checkable
class WorkerRunner(Protocol):
    """推理执行端口（线程实现 + 进程实现预留）。"""

    def submit(self, run: Callable[[CancelToken], None], *, deadline_ms: int,
               cancel: Optional[CancelToken] = None) -> None:
        """提交一次推理（异步；``run`` 在 worker 线程执行）。"""
        ...

    def restart(self) -> None:
        """超时/故障后清理/重启（线程级 = abandon）。"""
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
            self._threads.append(thread)
        thread.start()

    def restart(self) -> None:
        """线程级重启 = abandon（丢弃执行上下文；不阻塞后续 job/闸门）。"""
        with self._lock:
            self._restarts += 1
        self._log("vision worker restart（线程级：abandon 已超时 job 的执行上下文）")

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
