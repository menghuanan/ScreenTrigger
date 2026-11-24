# -*- coding: utf-8 -*-
"""HostClient：Python → Rust 宿主调用（TASK-005 批 B1，设计 §3.2/§5.3）。

- host_request id = "py-<uuid4>"（03）；仅白名单
  input_action/window_snapshot/acquire_input_lease/release_input_lease 可发起
  （03 明示），白名单外本地构造 INVALID_PARAMS、不占用 stdio；
- 等待 host_response 用条件变量：等待方（worker 线程）**不消费 stdin**——
  03 明令"需要等待 host_response 的 Python worker 不占 IPC reader"；
  host_response 由读循环（dispatcher）经 on_response 投递；
- 超时（默认 5s，06 普通 IPC 期限）→ INTERNAL_ERROR（message 含期限，
  对齐设计 §6.3/§9-D5 的超时错误码口径）；CAPABILITY_DENIED 原样传递；
- 迟到的 host_response（等待者已超时离开）→ stderr 记日志后丢弃。

骨架期无调用方（M01/M05 落地后接入），接口与单测就位。
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from backend.ipc.envelope import HOST_COMMAND_WHITELIST, build_host_request

Clock = Callable[[], float]
Logger = Callable[[str], None]


class HostCallResult:
    """request() 返回值：success/data/error 三态（data 与 error 互斥）。"""

    __slots__ = ("success", "data", "error")

    def __init__(self, success: bool, data: Any = None, error: Optional[Dict[str, Any]] = None) -> None:
        self.success: bool = success
        self.data: Any = data
        self.error: Optional[Dict[str, Any]] = error

    def __repr__(self) -> str:
        if self.success:
            return "HostCallResult(success=True, data=%r)" % (self.data,)
        return "HostCallResult(success=False, error=%r)" % (self.error,)


class _Waiter:
    """单次 host_response 等待位（条件变量共享状态）。"""

    __slots__ = ("done", "result")

    def __init__(self) -> None:
        self.done: bool = False
        self.result: Optional[HostCallResult] = None


class HostClient:
    """宿主调用客户端：send host_request + 条件变量等待 host_response。"""

    def __init__(
        self,
        transport: Any,
        session: Any,
        stderr: Any = None,
        clock: Optional[Clock] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        self._transport = transport
        self._session = session
        self._stderr = stderr if stderr is not None else sys.stderr
        # 单调时钟（可注入替身）；time.monotonic 不受系统时间调整影响（06）
        self._clock: Clock = clock if clock is not None else time.monotonic
        if logger is not None:
            self._log: Logger = logger
        else:
            target = self._stderr

            def _default_log(message: str) -> None:
                target.write("[sidecar] %s\n" % message)
                target.flush()

            self._log = _default_log
        self._cond = threading.Condition()
        self._waiters: Dict[str, _Waiter] = {}

    # -- 发起 -----------------------------------------------------------------

    def request(
        self,
        command: str,
        params: Dict[str, Any],
        timeout_s: float = 5.0,
    ) -> HostCallResult:
        """发起 host_request 并等待同 id 的 host_response（不占 IPC reader）。

        白名单外命令本地返回 INVALID_PARAMS（不产生任何 stdio 流量）；
        超时返回 INTERNAL_ERROR（message 含期限，§6.3 口径）。
        """
        if command not in HOST_COMMAND_WHITELIST:
            return HostCallResult(
                False,
                error={
                    "code": "INVALID_PARAMS",
                    "message": "host 命令白名单外",
                    "details": {"command": command},
                    "retryable": False,
                },
            )
        request_id = "py-" + str(uuid.uuid4())
        envelope = build_host_request(
            self._session.session_id, command, params, request_id=request_id
        )
        waiter = _Waiter()
        with self._cond:
            self._waiters[request_id] = waiter
        if not self._transport.send(envelope):
            with self._cond:
                self._waiters.pop(request_id, None)
            return HostCallResult(
                False,
                error={
                    "code": "INTERNAL_ERROR",
                    "message": "host_request 发送失败",
                    "details": {"request_id": request_id},
                    "retryable": True,
                },
            )
        deadline = self._clock() + timeout_s
        with self._cond:
            while not waiter.done:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if not waiter.done:
                self._waiters.pop(request_id, None)
        if not waiter.done:
            timeout_ms = int(timeout_s * 1000)
            self._log("host_request 超时（%dms）id=%s" % (timeout_ms, request_id))
            return HostCallResult(
                False,
                error={
                    "code": "INTERNAL_ERROR",
                    "message": "host_request 超时(%dms)" % timeout_ms,
                    "details": {"request_id": request_id, "timeout_ms": timeout_ms},
                    "retryable": False,
                },
            )
        assert waiter.result is not None  # on_response 置 done 时必已填 result
        return waiter.result

    # -- 投递（由 dispatcher 读循环调用） -----------------------------------------

    def on_response(self, envelope: Dict[str, Any]) -> None:
        """把 host_response 投递给等待者；无等待者（迟到）→ stderr 记日志丢弃。"""
        request_id = envelope.get("id")
        late = False
        with self._cond:
            waiter = self._waiters.pop(request_id, None)
            if waiter is None:
                late = True
            else:
                if envelope["success"]:
                    waiter.result = HostCallResult(True, data=envelope.get("data"))
                else:
                    waiter.result = HostCallResult(False, error=envelope.get("error"))
                waiter.done = True
                self._cond.notify_all()
        if late:
            self._log("迟到的 host_response，丢弃 id=%r" % (request_id,))
