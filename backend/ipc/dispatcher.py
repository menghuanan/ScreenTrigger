# -*- coding: utf-8 -*-
"""命令路由与 stdin 读循环托管（TASK-005 批 B1，设计 §3.2/§5.3/§6.5）。

- 框架命令表：hello/ping 由 main.py 注册（版本/时钟归属应用层）；
  query_request/cancel_request/cancel_job/get_job 由 dispatcher 内置实现；
  业务命令空注册表 → UNKNOWN_COMMAND（handler 属 M03–M07，设计 §1.2）。
- 读循环自 main.py 迁入：parse_frame（畸形计数）→ session 校验（丢弃，契约③）
  → 路由（request → 命令表；host_response → HostClient 条件变量）。
- 连续畸形计数对称语义（§6.5）：连续达 10（中间任何合法帧清零）→ stderr 记
  日志后退出 code 3，Rust 端经 EOF 既有路径关会话。

依赖注入：transport 只需 read_line()/send()（单测用内存替身）；时钟不进
dispatcher（ping 的时间戳由应用层 handler 产生）。
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

from backend.ipc.envelope import PROTOCOL_VERSION, MalformedFrameError, parse_frame
from backend.ipc.jobs import JobManager

Logger = Callable[[str], None]

# §6.5：连续畸形关会话阈值（03 数值冻结随设计 §9-D1；中间合法帧清零计数）
CONSECUTIVE_MALFORMED_LIMIT: int = 10
# §6.5：Python 侧连续畸形关会话以进程退出表达（Rust 端 EOF → 既有清理路径）
PROTOCOL_ERROR_EXIT_CODE: int = 3


class DispatcherError(Exception):
    """命令处理错误 → response.error（06 四字段结构）。"""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: Dict[str, Any] = details if details is not None else {}
        self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


class RequestLedger:
    """同会话请求台账：query_request 的数据源（03：只查询同会话）。

    容量对齐 03"请求 ID 最多保留最近 10000 个最终响应"的语义；骨架期响应
    同步完成，台账只有 completed/unknown 两态（pending 位留给异步命令任务）。
    """

    STATUS_COMPLETED = "completed"
    STATUS_UNKNOWN = "unknown"
    DEFAULT_CAPACITY = 10000

    def __init__(self, capacity: Optional[int] = None) -> None:
        self._lock = threading.Lock()
        self._capacity = capacity if capacity is not None else self.DEFAULT_CAPACITY
        self._entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def record(self, request_id: str, response: Dict[str, Any]) -> None:
        """记录一个最终响应（data 或 error dict）；LRU 淘汰最旧。"""
        with self._lock:
            self._entries.pop(request_id, None)
            self._entries[request_id] = response
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def query(self, request_id: str) -> Dict[str, Any]:
        """query_request data：{status:pending/completed/unknown, response}（03 逐字）。"""
        with self._lock:
            if request_id in self._entries:
                return {"status": self.STATUS_COMPLETED, "response": self._entries[request_id]}
            return {"status": self.STATUS_UNKNOWN, "response": None}

    def contains(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._entries


class Dispatcher:
    """入站帧路由器：request → 命令表；host_response → HostClient。"""

    def __init__(
        self,
        transport: Any,
        session: Any,
        stderr: Any = None,
        logger: Optional[Logger] = None,
        job_manager: Optional[JobManager] = None,
        hostclient: Any = None,
        consecutive_limit: Optional[int] = None,
        ledger_capacity: Optional[int] = None,
    ) -> None:
        self.transport = transport
        self.session = session
        self._stderr = stderr if stderr is not None else sys.stderr
        if logger is not None:
            self._log: Logger = logger
        else:
            target = self._stderr

            def _default_log(message: str) -> None:
                target.write("[sidecar] %s\n" % message)
                target.flush()

            self._log = _default_log
        self.jobs = job_manager if job_manager is not None else JobManager()
        self.hostclient = hostclient
        self.consecutive_limit = (
            consecutive_limit if consecutive_limit is not None else CONSECUTIVE_MALFORMED_LIMIT
        )
        self.ledger = RequestLedger(ledger_capacity)
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self.malformed_count: int = 0
        self.consecutive_malformed: int = 0

    # -- 注册表 ---------------------------------------------------------------

    def register(self, command: str, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """注册命令 handler（envelope → data）；重复注册覆盖（框架位后补实现用）。"""
        self._handlers[command] = handler

    # -- 响应发送 ---------------------------------------------------------------

    def send_response(
        self,
        request_id: str,
        success: bool,
        data: Any = None,
        error: Any = None,
    ) -> None:
        """发送 response 信封：成功含 data、失败含 error，两者互斥（03）。"""
        envelope: Dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session.session_id,
            "type": "response",
            "id": request_id,
            "success": success,
        }
        if success:
            envelope["data"] = data
        else:
            envelope["error"] = error
        if not self.transport.send(envelope):
            self._log("stdout 不可写，响应 id=%s 未送达" % request_id)

    # -- 命令路由（§5.3 命令表） -------------------------------------------------

    @staticmethod
    def _require_param(params: Dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value:
            raise DispatcherError(
                "INVALID_PARAMS",
                "参数 %s 必填且为字符串" % name,
                details={"field": name},
                retryable=False,
            )
        return value

    def _cmd_query_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.ledger.query(self._require_param(params, "request_id"))

    def _cmd_cancel_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # 幂等（03）；reason ∈ {completed, not_found, not_cancellable, cancelled}（§5.3）。
        # 骨架期响应同步完成：无在途可取消位 → 台账命中即 completed，否则 not_found。
        request_id = self._require_param(params, "request_id")
        if self.ledger.contains(request_id):
            return {"cancelled": False, "reason": "completed"}
        return {"cancelled": False, "reason": "not_found"}

    def _cmd_cancel_job(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job_id = self._require_param(params, "job_id")
        # 03：{cancelled:boolean}，终态取消为 false；未知 job_id 同样 false（03 未定义
        # cancel_job 的 NOT_FOUND，NOT_FOUND 仅 get_job 明示——见设计 §5.3）
        return {"cancelled": bool(self.jobs.cancel(job_id))}

    def _cmd_get_job(self, params: Dict[str, Any]) -> Dict[str, Any]:
        job_id = self._require_param(params, "job_id")
        snapshot = self.jobs.get(job_id)
        if snapshot is None:
            raise DispatcherError(
                "NOT_FOUND",
                "job 不存在",
                details={"job_id": job_id},
                retryable=False,
            )
        return snapshot

    def dispatch_command(self, envelope: Dict[str, Any]) -> None:
        """路由单条 request：命令表 → 响应 → 台账记账（query_request 数据源）。"""
        command = envelope.get("command")
        request_id = envelope["id"]
        params = envelope.get("params")
        if not isinstance(params, dict):
            params = {}
        success: bool
        payload: Dict[str, Any]
        handler = self._handlers.get(command) if isinstance(command, str) else None
        try:
            if handler is not None:
                success, payload = True, handler(envelope)
            elif command == "query_request":
                success, payload = True, self._cmd_query_request(params)
            elif command == "cancel_request":
                success, payload = True, self._cmd_cancel_request(params)
            elif command == "cancel_job":
                success, payload = True, self._cmd_cancel_job(params)
            elif command == "get_job":
                success, payload = True, self._cmd_get_job(params)
            else:
                # 业务命令空注册表：框架路由位已注册，handler 属后续任务（§1.2/§5.3）
                success = False
                payload = {
                    "code": "UNKNOWN_COMMAND",
                    "message": "未注册命令",
                    "details": {"command": command},
                    "retryable": False,
                }
        except DispatcherError as exc:
            success, payload = False, exc.to_dict()
        self.send_response(
            request_id,
            success,
            data=payload if success else None,
            error=None if success else payload,
        )
        self.ledger.record(request_id, payload)

    # -- 读循环（自 main.py 迁入，行为兼容） --------------------------------------

    def run(self) -> int:
        """stdin 读循环；返回进程退出码：0=EOF 优雅退出，3=连续畸形关会话（§6.5）。"""
        while True:
            try:
                raw = self.transport.read_line()
                if raw is None:
                    self._log("stdin EOF，优雅退出")
                    return 0
                envelope = parse_frame(raw)
            except MalformedFrameError as exc:
                self.malformed_count += 1
                self.consecutive_malformed += 1
                self._log("畸形帧 #%d（%s）" % (self.malformed_count, exc.reason))
                if self.consecutive_malformed >= self.consecutive_limit:
                    self._log(
                        "连续畸形帧达 %d，关闭会话（协议错误，退出 code %d）"
                        % (self.consecutive_limit, PROTOCOL_ERROR_EXIT_CODE)
                    )
                    return PROTOCOL_ERROR_EXIT_CODE
                continue
            # 任何结构合法帧清零连续计数（§6.5；契约③：session 丢弃帧结构合法）
            self.consecutive_malformed = 0
            if not self.session.verify(envelope):
                self._log("session_id 不匹配，丢弃该行：%r" % (envelope.get("session_id"),))
                continue
            msg_type = envelope.get("type")
            if msg_type == "request":
                self.dispatch_command(envelope)
            elif msg_type == "host_response":
                if self.hostclient is not None:
                    self.hostclient.on_response(envelope)
                else:
                    self._log("收到 host_response 但未配置 HostClient，忽略 id=%r" % envelope.get("id"))
            else:
                # 结构合法但 Python 侧不消费的错向帧（response/event 的流向是
                # Python→Rust）：只记日志，不计数、不响应
                self._log("忽略错向入站帧 type=%r id=%r" % (msg_type, envelope.get("id")))
