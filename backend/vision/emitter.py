# -*- coding: utf-8 -*-
"""识别事件端口与实现（TASK-013 §12）。

- **端口** ``RecognitionEmitter``：service 在 job 终态后调用（``completed``/``error``/``cancelled``）；
- **生产实现** ``StdioRecognitionEmitter``：把识别事件装入 03 信封，经单 writer 传输发送
  （``seq`` 取共享 ``HealthState.next_seq()``——**与 system_ready/catalog_changed 共用同一计数器**；
  Python 内部序号，UI seq 由 Rust 出口重分配）；
- **替身** ``NullRecognitionEmitter``（无副作用）与 ``RecordingRecognitionEmitter``（记录调用供断言）。

与 ``CatalogEmitter`` 是**不同端口、共享同一底层通道**（同一 transport/health/session/clock）。
基座**只发** ``recognition_completed``/``recognition_error``/``recognition_cancelled``；
``recognition_stats`` 归运行/监控层（TASK-022，Q8）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from ..ipc.envelope import PROTOCOL_VERSION, format_timestamp


@runtime_checkable
class RecognitionEmitter(Protocol):
    """识别事件端口（可注入替身）。"""

    def emit_completed(self, *, job_id: str, result: Any) -> None:
        """发射一次 ``recognition_completed``（payload=``{job_id, result}``）。"""
        ...

    def emit_error(self, *, job_id: str, error: Dict[str, Any]) -> None:
        """发射一次 ``recognition_error``（payload=``{job_id, error}``，error 为 06 四字段）。"""
        ...

    def emit_cancelled(self, *, job_id: str) -> None:
        """发射一次 ``recognition_cancelled``（payload=``{job_id}``）。"""
        ...


class StdioRecognitionEmitter:
    """生产实现：经 stdio 单 writer 传输发送识别事件帧。"""

    def __init__(
        self,
        transport: Any,
        health: Any,
        session: Any,
        clock: Callable[[], Any],
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._transport = transport
        self._health = health
        self._session = session
        self._clock = clock
        self._logger = logger

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def _emit(self, name: str, payload: Dict[str, Any]) -> None:
        envelope: Dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self._session.session_id,
            "type": "event",
            "seq": self._health.next_seq(),
            "name": name,
            "timestamp": format_timestamp(self._clock()),
            "payload": payload,
        }
        self._transport.send(envelope)
        self._log("%s 已发送（payload keys=%s）" % (name, ",".join(sorted(payload))))

    def emit_completed(self, *, job_id: str, result: Any) -> None:
        self._emit("recognition_completed", {"job_id": job_id, "result": result})

    def emit_error(self, *, job_id: str, error: Dict[str, Any]) -> None:
        self._emit("recognition_error", {"job_id": job_id, "error": error})

    def emit_cancelled(self, *, job_id: str) -> None:
        self._emit("recognition_cancelled", {"job_id": job_id})


class NullRecognitionEmitter:
    """无副作用替身（默认缺省：不装配 UI 通道时静默）。"""

    def emit_completed(self, *, job_id: str, result: Any) -> None:
        return None

    def emit_error(self, *, job_id: str, error: Dict[str, Any]) -> None:
        return None

    def emit_cancelled(self, *, job_id: str) -> None:
        return None


class RecordingRecognitionEmitter:
    """记录调用替身：``events`` 为 ``{name, job_id, result?, error?}`` 列表，供断言。"""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit_completed(self, *, job_id: str, result: Any) -> None:
        self.events.append({"name": "recognition_completed", "job_id": job_id, "result": result})

    def emit_error(self, *, job_id: str, error: Dict[str, Any]) -> None:
        self.events.append({"name": "recognition_error", "job_id": job_id, "error": error})

    def emit_cancelled(self, *, job_id: str) -> None:
        self.events.append({"name": "recognition_cancelled", "job_id": job_id})

    def names(self) -> List[str]:
        return [event["name"] for event in self.events]
