# -*- coding: utf-8 -*-
"""``catalog_changed`` 事件端口与实现（TASK-011 设计 §8）。

- **端口** ``CatalogEmitter``：service 在 ``Repository.commit`` **成功后**调用；
- **生产实现** ``StdioCatalogEmitter``：把 ``catalog_changed`` 事件装入 03 信封，
  经单 writer 传输发送（``seq`` 取 ``HealthState.next_seq()``、``session_id`` 取
  ``SessionState``、``timestamp`` 取 ``format_timestamp``）。Python seq 是内部序号，
  UI seq 由 Rust 出口统一重分配（03），此处不冒充；
- **单测替身** ``NullCatalogEmitter``（无副作用）与 ``RecordingCatalogEmitter``
  （记录调用供断言）。

Repository 侧**不得**依赖本模块（TASK-010 Q6：禁 Repository→UI）；``catalog_changed``
只在 service 提交成功后发（设计 §8：commit 抛错 → 不发射）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from ..ipc.envelope import PROTOCOL_VERSION, format_timestamp


@runtime_checkable
class CatalogEmitter(Protocol):
    """``catalog_changed`` 事件端口（可注入替身）。"""

    def emit_catalog_changed(self, *, generation: str, revision: int) -> None:
        """发射一次 ``catalog_changed``（payload=``{generation, revision}``）。"""
        ...


class StdioCatalogEmitter:
    """生产实现：经 stdio 单 writer 传输发送 ``catalog_changed`` 事件帧。

    Attributes:
        _transport: 具备 ``send(envelope) -> bool`` 的传输（单 writer 队列）。
        _health: 提供 ``next_seq()`` 的事件序号计数器（与 ``system_ready`` 同源）。
        _session: 提供 ``session_id`` 的会话状态。
        _clock: 返回 ``datetime`` 的墙钟（经 ``format_timestamp`` 归一为 UTC RFC3339）。
    """

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

    def emit_catalog_changed(self, *, generation: str, revision: int) -> None:
        """装入 03 事件信封并发送（不阻塞、不重排：单 writer 队列保证行完整）。"""
        envelope: Dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self._session.session_id,
            "type": "event",
            "seq": self._health.next_seq(),
            "name": "catalog_changed",
            "timestamp": format_timestamp(self._clock()),
            "payload": {"generation": generation, "revision": int(revision)},
        }
        self._transport.send(envelope)
        self._log(
            "catalog_changed 已发送（generation=%s revision=%d）" % (generation, revision)
        )


class NullCatalogEmitter:
    """无副作用替身（默认缺省：不装配 UI 通道时静默）。"""

    def emit_catalog_changed(self, *, generation: str, revision: int) -> None:
        """空实现（不发送任何内容）。"""
        return None


class RecordingCatalogEmitter:
    """记录调用替身：``events`` 为 ``{generation, revision}`` 列表，供断言。"""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit_catalog_changed(self, *, generation: str, revision: int) -> None:
        self.events.append({"generation": generation, "revision": int(revision)})
