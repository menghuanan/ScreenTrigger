# -*- coding: utf-8 -*-
"""协议响应与故障路由替身单测（设计 §6.3/§6.4 边界）。

覆盖：hello/ping 响应形状、system_ready 字段逐字对齐、UNKNOWN_COMMAND 四字段
错误、session_id 不匹配丢弃、畸形帧计数不关会话、6 种 fault 分支路由。
transport 全部用 io.StringIO/queue 替身，不 spawn 真进程；时钟与 lua 探针注入。
"""

import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.main import (  # noqa: E402
    CAPABILITIES,
    ENGINE_VERSION,
    FAULT_CRASH_EXIT_CODE,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    SidecarApp,
    SidecarArgs,
    StdioTransport,
    detect_lua_version,
    format_timestamp,
)

FIXED_MOMENT = datetime(2026, 10, 9, 12, 0, 5, tzinfo=timezone.utc)
SESSION_ID = "session-test-1"

# 逐字对齐设计 §4.3 的 7 项能力清单（含中文 reason），防止常量被无声改动
EXPECTED_CAPABILITIES = [
    {"id": "ocr", "state": "unavailable", "reason": "骨架未接入识别，模型未由引擎加载"},
    {"id": "lua_script", "state": "unavailable", "reason": "Lua 运行时未由骨架接入"},
    {"id": "template_match", "state": "unavailable", "reason": "骨架未实现"},
    {"id": "feature_match", "state": "unavailable", "reason": "ORB 未就绪"},
    {"id": "contour_match", "state": "unavailable", "reason": "骨架未实现"},
    {"id": "color_match", "state": "unavailable", "reason": "骨架未实现"},
    {"id": "input", "state": "unavailable", "reason": "M05 输入闸门未实现"},
    {"id": "capture", "state": "unavailable", "reason": "M01 捕获未实现"},
]


class CaptureBuffer(io.StringIO):
    """close() 置为空操作，使 stdout_close_after_ready 关流后仍可读取内容。"""

    def close(self) -> None:
        pass


def make_app(fault=None, lua_version="Lua 5.4", stdin_text=""):
    """组装 StringIO 替身 + 固定时钟 + 固定 lua 探针的 SidecarApp。"""
    out, err, stdin = CaptureBuffer(), io.StringIO(), io.StringIO(stdin_text)
    transport = StdioTransport(stdout=out, stdin=stdin, stderr=err)
    args = SidecarArgs(session_id=SESSION_ID, fault_scenario=fault)
    app = SidecarApp(
        args=args,
        transport=transport,
        clock=lambda: FIXED_MOMENT,
        stderr=err,
        lua_probe=lambda: lua_version,
    )
    return app, transport, out, err


def stdout_frames(out):
    """把 stdout 捕获文本按 NDJSON 拆帧（非空行逐行 json.loads）。"""
    return [json.loads(line) for line in out.getvalue().split("\n") if line]


def request_line(command, request_id, session_id=SESSION_ID):
    """构造一行入站 request 帧。"""
    return json.dumps(
        {
            "protocol_version": 1,
            "session_id": session_id,
            "type": "request",
            "id": request_id,
            "command": command,
            "params": {},
        }
    ) + "\n"


class SystemReadyTest(unittest.TestCase):
    """system_ready 事件：字段逐字对齐设计 §4.3①，正常路径先于一切响应。"""

    def test_module_capabilities_match_design_verbatim(self):
        self.assertEqual([dict(c) for c in CAPABILITIES], EXPECTED_CAPABILITIES)

    def test_ready_event_fields_match_design(self):
        app, _transport, out, err = make_app()
        self.assertEqual(app.run(), 0)
        frames = stdout_frames(out)
        self.assertEqual(len(frames), 1)
        ready = frames[0]
        self.assertEqual(set(ready.keys()),
                         {"protocol_version", "session_id", "type", "seq", "name", "timestamp", "payload"})
        self.assertEqual(ready["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(ready["session_id"], SESSION_ID)
        self.assertEqual(ready["type"], "event")
        self.assertEqual(ready["seq"], 1)
        self.assertEqual(ready["name"], "system_ready")
        self.assertEqual(ready["timestamp"], "2026-10-09T12:00:05.000Z")
        self.assertEqual(set(ready["payload"].keys()), {"capabilities", "versions"})
        self.assertEqual(ready["payload"]["capabilities"], EXPECTED_CAPABILITIES)
        self.assertEqual(len({cap["id"] for cap in ready["payload"]["capabilities"]}), len(EXPECTED_CAPABILITIES))
        for cap in ready["payload"]["capabilities"]:
            self.assertEqual(cap["state"], "unavailable")
            self.assertTrue(cap["reason"])
        self.assertEqual(ready["payload"]["versions"], {
            "protocol_version": PROTOCOL_VERSION,
            "engine_version": ENGINE_VERSION,
            "lua_version": "Lua 5.4",
            "schema_version": SCHEMA_VERSION,
        })
        self.assertEqual(app.health.phase, "exited")  # run() 经 stdin EOF 进终态（不可复活）

    def test_emit_system_ready_marks_health_ready(self):
        app, transport, _out, _err = make_app()
        app.transport.start()
        app.emit_system_ready()
        self.assertEqual(app.health.phase, "ready")  # 发送 system_ready 即 starting→ready
        self.assertEqual(app.health.event_seq, 1)
        transport.close_stdout()
        self.assertTrue(transport.join_writer(timeout=5.0))

    def test_lua_probe_failure_yields_null(self):
        app, _transport, out, _err = make_app(lua_version=None)
        self.assertEqual(app.run(), 0)
        ready = stdout_frames(out)[0]
        self.assertIsNone(ready["payload"]["versions"]["lua_version"])

    def test_real_probe_returns_string_or_none(self):
        # 真实 try-import 探针（D06：显式 lupa.lua54 后端）：
        # 探针环境装有 lupa → 恒为 "Lua 5.4"（default 后端的 5.5 不得出现）；干净环境 → None
        version = detect_lua_version()
        self.assertTrue(version is None or version == "Lua 5.4", msg="实测 %r 偏离 D06" % version)


class HelloPingTest(unittest.TestCase):
    """hello/ping 响应形状与 UNKNOWN_COMMAND 四字段错误（§4.3②③④）。"""

    def test_hello_response_shape(self):
        app, _transport, out, _err = make_app(stdin_text=request_line("hello", "req-1"))
        self.assertEqual(app.run(), 0)
        frames = stdout_frames(out)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["type"], "event")  # system_ready 先于响应（§6.1 步骤5）
        self.assertEqual(frames[0]["name"], "system_ready")
        response = frames[1]
        self.assertEqual(response["type"], "response")
        self.assertEqual(response["id"], "req-1")
        self.assertIs(response["success"], True)
        self.assertEqual(set(response.keys()),
                         {"protocol_version", "session_id", "type", "id", "success", "data"})
        data = response["data"]
        self.assertEqual(set(data.keys()), {
            "protocol_version", "engine_version", "lua_version",
            "schema_version", "capabilities", "model_manifests",
        })
        self.assertEqual(data["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(data["engine_version"], ENGINE_VERSION)
        self.assertEqual(data["lua_version"], "Lua 5.4")
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["capabilities"], EXPECTED_CAPABILITIES)
        self.assertEqual(data["model_manifests"], [])  # 骨架无模型，不伪造

    def test_ping_returns_injected_engine_time(self):
        app, _transport, out, _err = make_app(stdin_text=request_line("ping", "req-2"))
        self.assertEqual(app.run(), 0)
        response = stdout_frames(out)[-1]
        self.assertEqual(response["type"], "response")
        self.assertEqual(response["id"], "req-2")
        self.assertIs(response["success"], True)
        self.assertEqual(response["data"], {"engine_time": "2026-10-09T12:00:05.000Z"})

    def test_unknown_command_returns_four_field_error(self):
        app, _transport, out, _err = make_app(stdin_text=request_line("foo", "req-3"))
        self.assertEqual(app.run(), 0)
        response = stdout_frames(out)[-1]
        self.assertIs(response["success"], False)
        self.assertEqual(set(response.keys()),
                         {"protocol_version", "session_id", "type", "id", "success", "error"})
        error = response["error"]
        self.assertEqual(set(error.keys()), {"code", "message", "details", "retryable"})  # 06 四字段
        self.assertEqual(error["code"], "UNKNOWN_COMMAND")
        self.assertEqual(error["message"], "未注册命令")
        self.assertEqual(error["details"], {"command": "foo"})
        self.assertIs(error["retryable"], False)


class SessionGuardTest(unittest.TestCase):
    """session_id 校验与畸形帧计数（§4.3：不匹配→stderr 丢弃；畸形→仅计数）。"""

    def test_mismatched_session_is_dropped_with_stderr_log(self):
        app, _transport, out, err = make_app(
            stdin_text=request_line("hello", "req-1", session_id="session-other")
        )
        self.assertEqual(app.run(), 0)
        frames = stdout_frames(out)
        self.assertEqual(len(frames), 1)  # 只剩 system_ready，跨会话响应被丢弃
        self.assertEqual(frames[0]["name"], "system_ready")
        self.assertIn("session_id", err.getvalue())

    def test_malformed_frames_counted_but_session_kept(self):
        app, _transport, out, err = make_app(
            stdin_text="not-json-at-all\n" + request_line("ping", "req-2")
        )
        self.assertEqual(app.run(), 0)
        frames = stdout_frames(out)
        self.assertEqual(len(frames), 2)  # ready + ping 响应：会话未被关闭（TASK-005）
        self.assertEqual(frames[-1]["id"], "req-2")
        self.assertEqual(app.malformed_count, 1)
        self.assertIn("畸形帧", err.getvalue())


class FaultRoutingTest(unittest.TestCase):
    """6 种 --fault-scenario 分支路由（§6.2）。"""

    def test_crash_on_start_exits_before_any_output(self):
        app, _transport, out, _err = make_app(fault="crash_on_start")
        self.assertEqual(app.run(), FAULT_CRASH_EXIT_CODE)
        self.assertEqual(out.getvalue(), "")  # 不产出任何协议帧
        self.assertEqual(app.health.phase, "exited")

    def test_silent_no_ready_sends_nothing(self):
        app, _transport, out, _err = make_app(fault="silent_no_ready")
        self.assertEqual(app.run(), 0)
        self.assertEqual(out.getvalue(), "")  # 宿主 30s 看门狗应判 startup_timeout
        self.assertEqual(app.health.event_seq, 0)  # 从未发出任何事件（未 ready）
        self.assertEqual(app.health.exit_reason, "stdin_eof")  # 经 EOF 优雅退出

    def test_stderr_flood_writes_bounded_lines_then_normal_flow(self):
        app, _transport, out, err = make_app(fault="stderr_flood")
        app.stderr_flood_lines = 5  # 单测调小洪流量（默认 100k）
        self.assertEqual(app.run(), 0)
        flood_lines = [line for line in err.getvalue().split("\n") if "stderr-flood" in line]
        self.assertEqual(len(flood_lines), 5)
        frames = stdout_frames(out)
        self.assertEqual(frames[0]["name"], "system_ready")  # 洪泛后流程照常

    def test_stdout_close_after_ready_closes_stream(self):
        app, transport, out, _err = make_app(fault="stdout_close_after_ready")
        saved_stdout = os.dup(1)
        try:
            self.assertEqual(app.run(), 0)
            with self.assertRaises(OSError):
                os.fstat(1)
        finally:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        frames = stdout_frames(out)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["name"], "system_ready")
        self.assertFalse(transport.send({"after": "close"}))  # 关闭后拒绝再发送

    def test_exit_after_ready_flushes_ready_then_exits(self):
        app, _transport, out, _err = make_app(fault="exit_after_ready")
        self.assertEqual(app.run(), FAULT_CRASH_EXIT_CODE)
        frames = stdout_frames(out)
        self.assertEqual(len(frames), 1)  # ready 帧在退出前已 flush
        self.assertEqual(frames[0]["name"], "system_ready")
        self.assertEqual(app.health.phase, "exited")

    def test_stdout_garbage_lines_precede_ready(self):
        app, _transport, out, _err = make_app(fault="stdout_garbage")
        self.assertEqual(app.run(), 0)
        raw_lines = [line for line in out.getvalue().split("\n") if line]
        self.assertEqual(len(raw_lines), 4)
        self.assertEqual(raw_lines[0], "this is not json at all")
        self.assertEqual(raw_lines[1], '{"protocol_version":1,"session_id":')
        self.assertEqual(raw_lines[2], "]]}{{{")
        ready = json.loads(raw_lines[3])
        self.assertEqual(ready["name"], "system_ready")


class ClockTest(unittest.TestCase):
    """可注入时钟与 RFC3339 毫秒格式。"""

    def test_format_timestamp_millisecond_precision(self):
        self.assertEqual(format_timestamp(FIXED_MOMENT), "2026-10-09T12:00:05.000Z")
        self.assertEqual(
            format_timestamp(datetime(2026, 1, 2, 3, 4, 5, 678900, tzinfo=timezone.utc)),
            "2026-01-02T03:04:05.678Z",
        )

    def test_naive_datetime_treated_as_utc(self):
        self.assertEqual(format_timestamp(datetime(2026, 10, 9, 12, 0, 5)), "2026-10-09T12:00:05.000Z")


if __name__ == "__main__":
    unittest.main()
