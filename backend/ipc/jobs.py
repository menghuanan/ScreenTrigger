# -*- coding: utf-8 -*-
"""JobManager：异步 job 生命周期框架（TASK-005 批 B1，设计 §3.2/§5.3）。

- job_id = "job-<uuid4>"（03 风格）；
- 状态机：pending → running → completed/error；pending/running → cancelled；
- get_job data = {status, result, error}（03 逐字）；未知 job_id → None
  （dispatcher 层转 NOT_FOUND，06）；
- cancel：终态/未知 → False（03：终态取消为 false）。

骨架期无真实 job（识别 job 属后续任务），框架与单测就位；状态迁移经
threading.Lock 串行化，读快照与写迁移线程安全。
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable, Dict, FrozenSet, Optional

Status = str
Snapshot = Dict[str, Any]


class JobManager:
    """异步 job 注册表与状态机（无真实业务 job 的框架位）。"""

    STATUS_PENDING: Status = "pending"
    STATUS_RUNNING: Status = "running"
    STATUS_COMPLETED: Status = "completed"
    STATUS_CANCELLED: Status = "cancelled"
    STATUS_ERROR: Status = "error"

    TERMINAL_STATUSES: FrozenSet[Status] = frozenset(
        {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_ERROR}
    )

    def __init__(self, job_id_factory: Optional[Callable[[], str]] = None) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        # 可注入 id 工厂（单测确定性）；缺省 job-<uuid4>（03 风格）
        self._job_id_factory: Callable[[], str] = (
            job_id_factory if job_id_factory is not None else self._default_job_id
        )

    @staticmethod
    def _default_job_id() -> str:
        return "job-" + str(uuid.uuid4())

    # -- 查询 ---------------------------------------------------------------

    @staticmethod
    def _snapshot(state: Dict[str, Any]) -> Snapshot:
        """get_job data 形状：{status, result, error}，仅此三字段（03 逐字）。"""
        return {"status": state["status"], "result": state["result"], "error": state["error"]}

    def get(self, job_id: str) -> Optional[Snapshot]:
        """未知 job_id 返回 None（dispatcher 转 NOT_FOUND）；否则返回快照。"""
        with self._lock:
            state = self._jobs.get(job_id)
            return self._snapshot(state) if state is not None else None

    def is_terminal(self, status: Status) -> bool:
        """终态判定：completed/cancelled/error（终态不可复活，06）。"""
        return status in self.TERMINAL_STATUSES

    # -- 生命周期迁移 ---------------------------------------------------------

    def create_job(self, kind: Optional[str] = None) -> str:
        """创建 job（pending）；返回 job_id。kind 仅供诊断，不出 get_job data。"""
        with self._lock:
            job_id = self._job_id_factory()
            while job_id in self._jobs:  # uuid 撞车防御（概率可忽略）
                job_id = self._job_id_factory()
            self._jobs[job_id] = {
                "status": self.STATUS_PENDING,
                "result": None,
                "error": None,
                "kind": kind,
            }
            return job_id

    def _transition(
        self,
        job_id: str,
        new_status: Status,
        allowed_from: FrozenSet[Status],
        result: Any = None,
        error: Any = None,
    ) -> bool:
        """状态迁移原语：未知 job 或当前状态不在 allowed_from → False。"""
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None or state["status"] not in allowed_from:
                return False
            state["status"] = new_status
            if result is not None:
                state["result"] = result
            if error is not None:
                state["error"] = error
            return True

    def mark_running(self, job_id: str) -> bool:
        """pending → running。"""
        return self._transition(
            job_id, self.STATUS_RUNNING, frozenset({self.STATUS_PENDING})
        )

    def complete(self, job_id: str, result: Any) -> bool:
        """pending/running → completed（终态，不可再迁移）。"""
        return self._transition(
            job_id,
            self.STATUS_COMPLETED,
            frozenset({self.STATUS_PENDING, self.STATUS_RUNNING}),
            result=result,
        )

    def fail(self, job_id: str, error: Dict[str, Any]) -> bool:
        """pending/running → error（终态；error 为 06 四字段错误结构）。"""
        return self._transition(
            job_id,
            self.STATUS_ERROR,
            frozenset({self.STATUS_PENDING, self.STATUS_RUNNING}),
            error=error,
        )

    def cancel(self, job_id: str) -> bool:
        """pending/running → cancelled；终态/未知 → False（03：终态取消为 false）。"""
        return self._transition(
            job_id,
            self.STATUS_CANCELLED,
            frozenset({self.STATUS_PENDING, self.STATUS_RUNNING}),
        )
