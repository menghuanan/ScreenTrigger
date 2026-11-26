# -*- coding: utf-8 -*-
"""Vision 取消原语（TASK-013 §7.1；本地结构化定义，**不 import backend.capture**）。

00「允许依赖」表未列 ``Vision→Capture``；为不越界且**不改** capture，Vision 定义
**结构等价**的本地端口（结构类型天然兼容 01「所有长调用接收 CancelToken」）：

- ``CancelToken``：只要求 ``is_cancelled()`` / ``throw_if_cancelled()``；
- ``CancelledError``：取消不是 06 错误码，以异常表达（上层映射为 cancelled 状态）；
- ``NullCancelToken``：永不取消（无取消源场景）；
- ``CancelFlag``：线程安全的手动取消位（超时/外部取消共享）；
- ``JobBoundCancelToken``：``is_cancelled()`` = 本地 ``CancelFlag`` **或** 共享
  ``JobManager.get(job_id)["status"] == "cancelled"``——**复用既有 ``cancel_job`` 通道**，
  无需改造 ``jobs.py``/``dispatcher.py``。

取消语义（06）：取消不用错误码；迟到结果只丢弃、不复活终态。
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable


class CancelledError(Exception):
    """协作式取消信号（不是 06 错误码；上层映射为 cancelled 状态）。"""


@runtime_checkable
class CancelToken(Protocol):
    """长调用取消令牌最小接口（与 ``backend.capture.cancel.CancelToken`` 结构等价）。"""

    def is_cancelled(self) -> bool:
        """已请求取消返回 True。"""
        ...

    def throw_if_cancelled(self) -> None:
        """已取消则抛 ``CancelledError``，否则返回。"""
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
            raise CancelledError("操作已取消")


class JobBoundCancelToken:
    """把 ``cancel_job`` 通道与本地取消位合成一个取消令牌。

    ``is_cancelled()`` 读共享 ``JobManager`` 的 job 状态；``cancel_job``（dispatcher
    内置）无需改造即经 job 状态**天然**传导到 provider 的协作式取消点（§6.3/§7.1）。
    """

    def __init__(self, job_id: str, jobs: Any, local: CancelFlag) -> None:
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job_id 必须为非空字符串")
        if jobs is None:
            raise ValueError("jobs 不能为空")
        self._job_id = job_id
        self._jobs = jobs
        self._local: CancelFlag = local if local is not None else CancelFlag()

    def is_cancelled(self) -> bool:
        if self._local.is_cancelled():
            return True
        snapshot = self._jobs.get(self._job_id)
        if snapshot is None:
            return True
        return snapshot.get("status") == "cancelled"

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise CancelledError("操作已取消")
