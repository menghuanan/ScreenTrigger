# -*- coding: utf-8 -*-
"""TASK-005 Python 契约测试（设计 §8.1）：与 Rust contract_tests 共跑
schema/contract-cases.json 同一份用例集。

- transport 用内存替身（MemoryTransport / io.StringIO），不 spawn 真进程；
- 时钟注入（ping 的 engine_time、HostClient 超时）；
- 阈值断言一律从 cases 的 thresholds 读取比对，不另行硬编码（schema/README 规则）；
- 兼容回归：main.py re-export 的信封接口与 backend.ipc.envelope 同一对象。
"""

import io
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.ipc.dispatcher import (  # noqa: E402
    CONSECUTIVE_MALFORMED_LIMIT,
    PROTOCOL_ERROR_EXIT_CODE,
    Dispatcher,
    RequestLedger,
)
from backend.ipc.envelope import HOST_COMMAND_WHITELIST, MAX_FRAME_BYTES  # noqa: E402
from backend.ipc.hostclient import HostClient  # noqa: E402
from backend.ipc.jobs import JobManager  # noqa: E402
from backend.main import (  # noqa: E402
    format_timestamp,
    parse_frame,
    encode_envelope,
)
from backend.main import StdioTransport  # noqa: E402  # noqa: E402
from backend.main import FrameTooLargeError  # noqa: E402
from backend.main import SessionState  # noqa: E402
import backend.main as main_module  # noqa: E402
import backend.ipc.envelope as envelope_module  # noqa: E402

FIXED_MOMENT = datetime(2026, 10, 9, 12, 0, 5, tzinfo=timezone.utc)
SESSION_ID = "session-contract-1"

with open(os.path.join(_REPO_ROOT, "schema", "contract-cases.json"), "r", encoding="utf-8") as fh:
    CONTRACT = json.load(fh)


def cases_by_kind(kind, applies_to="python"):
    """取 applies_to 含本端且 kind 匹配的用例（双端共跑同一份文件）。"""
    return [c for c in CONTRACT["cases"] if c["kind"] == kind and applies_to in c["applies_to"]]


class MemoryTransport:
    """进程内传输替身：inbound 行队列 + sent 信封按序记录（单 writer 语义）。"""

    def __init__(self, inbound_lines=None):
        self._inbound = list(inbound_lines or [])
        self.sent = []      # send() 记录信封 dict
        self.sent_raw = []  # send_raw() 记录原始行
        self.closed = False

    def send(self, envelope):
        if self.closed:
            return False
        self.sent.append(envelope)
        return True

    def send_raw(self, frame):
        if self.closed:
            return False
        self.sent_raw.append(frame)
        return True

    def read_line(self):
        return self._inbound.pop(0) if self._inbound else None

    def close_stdout(self, close_stream=False):
        self.closed = True

    def join_writer(self, timeout=None):
        return True


def make_dispatcher(stdin_lines=None, register_clocked_handlers=True):
    """组装内存替身 Dispatcher（含 JobManager/HostClient/stderr 捕获）。"""
    stderr = io.StringIO()
    transport = MemoryTransport(stdin_lines)
    session = SessionState(SESSION_ID)
    dispatcher = Dispatcher(
        transport=transport,
        session=session,
        stderr=stderr,
        job_manager=JobManager(),
        hostclient=HostClient(transport, session, stderr=stderr),
    )
    if register_clocked_handlers:
        dispatcher.register(
            "hello",
            lambda envelope: {
                "protocol_version": 1,
                "engine_version": "0.1.0-skeleton",
                "lua_version": "Lua 5.4",
                "schema_version": 2,
                "capabilities": [],
                "model_manifests": [],
            },
        )
        dispatcher.register(
            "ping", lambda envelope: {"engine_time": format_timestamp(FIXED_MOMENT)}
        )
    return dispatcher, transport, stderr


def request_line(command, request_id, session_id=SESSION_ID, params=None, **extra):
    envelope = {
        "protocol_version": 1,
        "session_id": session_id,
        "type": "request",
        "id": request_id,
        "command": command,
        "params": params if params is not None else {},
    }
    envelope.update(extra)
    return json.dumps(envelope) + "\n"


class ThresholdSyncTest(unittest.TestCase):
    """thresholds 与代码常量一致（schema/README：禁止测试侧另硬编码数值）。"""

    def test_thresholds_match_code_constants(self):
        thresholds = CONTRACT["thresholds"]
        self.assertEqual(thresholds["maxFrameBytes"], MAX_FRAME_BYTES)
        self.assertEqual(thresholds["consecutiveMalformedLimit"], CONSECUTIVE_MALFORMED_LIMIT)
        self.assertEqual(
            thresholds["pythonConsecutiveGarbageExitCode"], PROTOCOL_ERROR_EXIT_CODE
        )
        self.assertEqual(thresholds["requestLedgerCapacity"], RequestLedger.DEFAULT_CAPACITY)
        self.assertEqual(thresholds["hostWhitelist"], list(HOST_COMMAND_WHITELIST))
        self.assertEqual(thresholds["protocolVersion"], 1)
        self.assertEqual(thresholds["normalDeadlineMs"], 5000)
        self.assertEqual(thresholds["startupDeadlineMs"], 30000)


class ValidFrameContractTest(unittest.TestCase):
    """kind=valid_frame：schema 必填集内全部样本可解析且往返一致。"""

    def test_valid_frames_parse_roundtrip(self):
        for case in cases_by_kind("valid_frame"):
            line = json.dumps(case["frame"]) + "\n"
            self.assertEqual(parse_frame(line), case["frame"], msg=case["id"])


class MalformedContractTest(unittest.TestCase):
    """kind=malformed：畸形原因串与 expect.reason 逐字一致。"""

    def test_malformed_raw_lines(self):
        for case in cases_by_kind("malformed"):
            if "raw" not in case:
                continue
            with self.assertRaises(Exception) as ctx:  # noqa: B017
                parse_frame(case["raw"])
            self.assertEqual(ctx.exception.reason, case["expect"]["reason"], msg=case["id"])

    def test_malformed_frames(self):
        for case in cases_by_kind("malformed"):
            if "frame" not in case:
                continue
            line = json.dumps(case["frame"])
            with self.assertRaises(Exception) as ctx:  # noqa: B017
                parse_frame(line)
            self.assertEqual(ctx.exception.reason, case["expect"]["reason"], msg=case["id"])
            self.assertTrue(case["expect"]["counted"], msg=case["id"])


class DropNoCountContractTest(unittest.TestCase):
    """kind=drop_no_count（用户冻结契约③）：丢弃+日志，不计畸形、不响应。"""

    def test_missing_and_stale_session_dropped_without_counting(self):
        for case in cases_by_kind("drop_no_count"):
            dispatcher, transport, stderr = make_dispatcher(
                stdin_lines=[json.dumps(case["frame"]) + "\n"]
            )
            self.assertEqual(dispatcher.run(), 0, msg=case["id"])
            self.assertEqual(dispatcher.malformed_count, 0, msg=case["id"])
            self.assertEqual(dispatcher.consecutive_malformed, 0, msg=case["id"])
            self.assertEqual(transport.sent, [], msg=case["id"])
            self.assertIn("session_id", stderr.getvalue(), msg=case["id"])


class OversizeContractTest(unittest.TestCase):
    """kind=oversize：发送端拒绝发送 + PROTOCOL_ERROR 日志，会话不中断。"""

    def test_oversize_frame_rejected_on_send_and_session_stays_alive(self):
        out, err = io.StringIO(), io.StringIO()
        transport = StdioTransport(stdout=out, stdin=io.StringIO(), stderr=err)
        oversized = {"payload": "x" * (MAX_FRAME_BYTES + 1)}
        self.assertFalse(transport.send(oversized))
        self.assertIn("PROTOCOL_ERROR", err.getvalue())
        self.assertEqual(out.getvalue(), "")  # 超限帧不产出任何 stdout 输出
        # 会话存活：合法小帧仍可发送（03：超限中止该消息，不断开会话）
        self.assertTrue(transport.send({"ok": 1}))


class DispatchContractTest(unittest.TestCase):
    """kind=dispatch：框架命令响应形状（03 逐字）与业务命令 UNKNOWN_COMMAND。"""

    def _run_one(self, lines):
        dispatcher, transport, stderr = make_dispatcher(stdin_lines=lines)
        self.assertEqual(dispatcher.run(), 0)
        return dispatcher, transport

    def test_hello_and_ping_shapes(self):
        dispatcher, transport = self._run_one(
            [request_line("hello", "ui-1"), request_line("ping", "ui-2")]
        )
        self.assertEqual(len(transport.sent), 2)
        hello = transport.sent[0]
        self.assertEqual(hello["type"], "response")
        self.assertEqual(hello["id"], "ui-1")
        self.assertIs(hello["success"], True)
        self.assertEqual(set(hello["data"].keys()), {
            "protocol_version", "engine_version", "lua_version",
            "schema_version", "capabilities", "model_manifests",
        })
        ping = transport.sent[1]
        self.assertEqual(ping["data"], {"engine_time": "2026-10-09T12:00:05.000Z"})

    def test_business_command_returns_unknown_command(self):
        for case in cases_by_kind("dispatch"):
            if case.get("command") != "execute_script":
                continue
            dispatcher, transport = self._run_one(
                [request_line(case["command"], "ui-3", params={})]
            )
            response = transport.sent[0]
            self.assertIs(response["success"], False, msg=case["id"])
            error = response["error"]
            self.assertEqual(set(error.keys()), {"code", "message", "details", "retryable"})
            self.assertEqual(error["code"], case["expect"]["error"]["code"], msg=case["id"])
            self.assertEqual(error["message"], case["expect"]["error"]["message"], msg=case["id"])
            self.assertIs(error["retryable"], case["expect"]["error"]["retryable"], msg=case["id"])

    def test_get_job_not_found(self):
        dispatcher, transport = self._run_one(
            [request_line("get_job", "ui-4", params={"job_id": "job-missing"})]
        )
        response = transport.sent[0]
        self.assertIs(response["success"], False)
        self.assertEqual(response["error"]["code"], "NOT_FOUND")
        self.assertEqual(response["error"]["details"], {"job_id": "job-missing"})

    def test_cancel_request_completed_and_not_found(self):
        # 台账属 dispatcher 实例：同一实例内先记账再喂入两帧
        dispatcher, transport, stderr = make_dispatcher(
            stdin_lines=[
                request_line("cancel_request", "ui-5", params={"request_id": "known-id"}),
                request_line("cancel_request", "ui-6", params={"request_id": "never-seen"}),
            ]
        )
        dispatcher.ledger.record("known-id", {"status": "completed"})  # 已有最终响应
        self.assertEqual(dispatcher.run(), 0)
        self.assertEqual(transport.sent[0]["data"], {"cancelled": False, "reason": "completed"})
        self.assertEqual(transport.sent[1]["data"], {"cancelled": False, "reason": "not_found"})

    def test_cancel_job_terminal_false(self):
        dispatcher, transport = self._run_one([])
        job_id = dispatcher.jobs.create_job()
        dispatcher.jobs.complete(job_id, {"done": True})  # 终态
        dispatcher, transport = self._run_one(
            [request_line("cancel_job", "ui-7", params={"job_id": job_id})]
        )
        self.assertEqual(transport.sent[0]["data"], {"cancelled": False})

    def test_query_request_completed_then_unknown(self):
        # 同一 dispatcher 实例：先 ping（ui-8 完成并记账），再查询 ui-8 / ui-never
        dispatcher, transport, stderr = make_dispatcher(
            stdin_lines=[
                request_line("ping", "ui-8"),
                request_line("query_request", "ui-9", params={"request_id": "ui-8"}),
                request_line("query_request", "ui-10", params={"request_id": "ui-never"}),
            ]
        )
        self.assertEqual(dispatcher.run(), 0)
        self.assertEqual(transport.sent[1]["data"]["status"], "completed")
        self.assertEqual(transport.sent[1]["data"]["response"],
                         {"engine_time": "2026-10-09T12:00:05.000Z"})
        self.assertEqual(transport.sent[2]["data"], {"status": "unknown", "response": None})


class ConsecutiveMalformedContractTest(unittest.TestCase):
    """kind=threshold（consecutive-malformed-close-session）：连续 10 → code 3；
    中间合法帧清零计数（§6.5）。"""

    def _garbage(self, n):
        return ["not-json-at-all\n"] * n

    def test_below_threshold_keeps_session(self):
        limit = CONSECUTIVE_MALFORMED_LIMIT
        dispatcher, transport, stderr = make_dispatcher(
            stdin_lines=self._garbage(limit - 1)
        )
        self.assertEqual(dispatcher.run(), 0)  # EOF 正常退出
        self.assertEqual(dispatcher.malformed_count, limit - 1)

    def test_reaching_threshold_returns_code_3(self):
        limit = CONSECUTIVE_MALFORMED_LIMIT
        dispatcher, transport, stderr = make_dispatcher(stdin_lines=self._garbage(limit))
        self.assertEqual(dispatcher.run(), PROTOCOL_ERROR_EXIT_CODE)
        self.assertIn("连续畸形", stderr.getvalue())

    def test_valid_frame_resets_consecutive_counter(self):
        limit = CONSECUTIVE_MALFORMED_LIMIT
        lines = self._garbage(limit - 1) + [request_line("ping", "ui-r")] + self._garbage(1)
        dispatcher, transport, stderr = make_dispatcher(stdin_lines=lines)
        self.assertEqual(dispatcher.run(), 0)  # 未达连续阈值：合法帧已清零
        self.assertEqual(dispatcher.malformed_count, limit)  # 总数累计
        self.assertEqual(len(transport.sent), 1)  # ping 响应照常
        self.assertEqual(transport.sent[0]["id"], "ui-r")


class JobLifecycleTest(unittest.TestCase):
    """JobManager 状态机（骨架框架位）：迁移合法性、终态不可复活、NOT_FOUND。"""

    def test_lifecycle_pending_running_completed(self):
        jobs = JobManager()
        job_id = jobs.create_job()
        self.assertTrue(job_id.startswith("job-"))
        self.assertEqual(jobs.get(job_id), {"status": "pending", "result": None, "error": None})
        self.assertTrue(jobs.mark_running(job_id))
        self.assertTrue(jobs.complete(job_id, {"matches": 3}))
        self.assertEqual(
            jobs.get(job_id), {"status": "completed", "result": {"matches": 3}, "error": None}
        )

    def test_fail_sets_error_field(self):
        jobs = JobManager()
        job_id = jobs.create_job()
        error = {"code": "RECOGNITION_FAILED", "message": "推理失败",
                 "details": {}, "retryable": False}
        self.assertTrue(jobs.fail(job_id, error))
        self.assertEqual(jobs.get(job_id)["status"], "error")
        self.assertEqual(jobs.get(job_id)["error"], error)

    def test_terminal_cancel_is_false_and_unknown_not_found(self):
        jobs = JobManager()
        job_id = jobs.create_job()
        self.assertTrue(jobs.complete(job_id, None))
        self.assertFalse(jobs.cancel(job_id))  # 03：终态取消为 false
        self.assertIsNone(jobs.get("job-missing"))  # dispatcher 层转 NOT_FOUND


class HostClientContractTest(unittest.TestCase):
    """HostClient：白名单、roundtrip、超时（注入时钟）、迟到响应丢弃。"""

    def test_whitelist_denied_locally_without_traffic(self):
        dispatcher, transport, stderr = make_dispatcher()
        result = dispatcher.hostclient.request("click_mouse", {"x": 1, "y": 2})
        self.assertFalse(result.success)
        self.assertEqual(result.error["code"], "INVALID_PARAMS")
        self.assertEqual(transport.sent, [])  # 白名单外不产生任何 stdio 流量
        for command in HOST_COMMAND_WHITELIST:
            self.assertIn(command, CONTRACT["thresholds"]["hostWhitelist"])

    def test_request_roundtrip_via_condition_variable(self):
        dispatcher, transport, stderr = make_dispatcher()
        results = []

        def worker():
            results.append(
                dispatcher.hostclient.request("window_snapshot", {}, timeout_s=5.0)
            )

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 2.0
        while not transport.sent and time.time() < deadline:
            time.sleep(0.005)  # 等 host_request 帧出现（waiter 已注册后才 send）
        self.assertTrue(transport.sent)
        frame = transport.sent[0]
        self.assertEqual(frame["type"], "host_request")
        self.assertTrue(frame["id"].startswith("py-"))
        self.assertEqual(frame["command"], "window_snapshot")
        dispatcher.hostclient.on_response({
            "protocol_version": 1,
            "session_id": SESSION_ID,
            "type": "host_response",
            "id": frame["id"],
            "success": True,
            "data": {"ok": True},
        })
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].data, {"ok": True})

    def test_capability_denied_error_is_passed_through(self):
        dispatcher, transport, stderr = make_dispatcher()
        results = []

        def worker():
            results.append(
                dispatcher.hostclient.request("acquire_input_lease", {}, timeout_s=5.0)
            )

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.time() + 2.0
        while not transport.sent and time.time() < deadline:
            time.sleep(0.005)
        frame = transport.sent[0]
        dispatcher.hostclient.on_response({
            "protocol_version": 1,
            "session_id": SESSION_ID,
            "type": "host_response",
            "id": frame["id"],
            "success": False,
            "error": {"code": "CAPABILITY_DENIED", "message": "宿主能力未提供",
                      "details": {"command": "acquire_input_lease"}, "retryable": False},
        })
        thread.join(2.0)
        self.assertFalse(results[0].success)
        self.assertEqual(results[0].error["code"], "CAPABILITY_DENIED")

    def test_timeout_with_injected_clock(self):
        dispatcher, transport, stderr = make_dispatcher()
        clock_values = iter([0.0, 999.0])  # deadline=0+5；首轮 remaining<0 → 立即超时
        client = HostClient(
            transport,
            dispatcher.session,
            stderr=stderr,
            clock=lambda: next(clock_values, 999.0),
        )
        result = client.request("input_action", {"action_id": "a1"}, timeout_s=5.0)
        self.assertFalse(result.success)
        self.assertEqual(result.error["code"], "INTERNAL_ERROR")  # §6.3/§9-D5 口径
        self.assertEqual(result.error["details"]["timeout_ms"], 5000)
        self.assertEqual(len(transport.sent), 1)  # 请求已发出，等不到响应

    def test_late_response_is_logged_and_dropped(self):
        dispatcher, transport, stderr = make_dispatcher()
        dispatcher.hostclient.on_response({
            "protocol_version": 1,
            "session_id": SESSION_ID,
            "type": "host_response",
            "id": "py-nobody",
            "success": True,
            "data": {},
        })
        self.assertIn("迟到的 host_response", stderr.getvalue())


class EnvelopeMigrationRegressionTest(unittest.TestCase):
    """平移回归：main.py re-export 与 backend.ipc.envelope 为同一对象（51 旧测不迁移即绿）。"""

    def test_reexports_are_the_moved_objects(self):
        self.assertIs(main_module.encode_envelope, envelope_module.encode_envelope)
        self.assertIs(main_module.parse_frame, envelope_module.parse_frame)
        self.assertIs(main_module.format_timestamp, envelope_module.format_timestamp)
        self.assertIs(main_module.FrameTooLargeError, envelope_module.FrameTooLargeError)
        self.assertIs(main_module.MalformedFrameError, envelope_module.MalformedFrameError)
        self.assertIs(main_module.MAX_FRAME_BYTES, envelope_module.MAX_FRAME_BYTES)
        self.assertIs(main_module.PROTOCOL_VERSION, envelope_module.PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
