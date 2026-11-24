# -*- coding: utf-8 -*-
"""信封编码/解析替身单测（设计 §3.4/§6.3）。

覆盖：序列化紧凑单行 LF 结尾、内容换行 JSON 转义、2MiB 上限拒绝、
畸形 JSON、缺必填字段、字段类型/版本/消息类型校验，以及 StdioTransport
的单 writer 队列行为（io.StringIO 替身，不 spawn 真进程）。
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.main import (  # noqa: E402
    MAX_FRAME_BYTES,
    FrameTooLargeError,
    MalformedFrameError,
    StdioTransport,
    configure_stdio,
    encode_envelope,
    parse_frame,
)


def _request(**overrides):
    """构造一条合法 request 信封，允许按字段覆盖。"""
    envelope = {
        "protocol_version": 1,
        "session_id": "session-test-1",
        "type": "request",
        "id": "req-1",
        "command": "hello",
        "params": {},
    }
    envelope.update(overrides)
    return envelope


class EncodeEnvelopeTest(unittest.TestCase):
    """encode_envelope：紧凑单行、LF 结尾、转义与 2MiB 上限。"""

    def test_encodes_single_lf_terminated_compact_line(self):
        frame = encode_envelope(_request())
        self.assertTrue(frame.endswith("\n"))
        self.assertEqual(frame.count("\n"), 1)  # 整帧只有结尾一个 LF
        self.assertNotIn("\n", frame[:-1])
        self.assertEqual(json.loads(frame), _request())

    def test_content_newline_is_json_escaped(self):
        envelope = {"payload": {"note": "line1\nline2"}}
        frame = encode_envelope(envelope)
        self.assertEqual(frame.count("\n"), 1)  # 内容换行不产生新帧
        self.assertIn("line1\\nline2", frame)  # 由 JSON 转义承载
        self.assertEqual(json.loads(frame), envelope)  # 往返一致

    def test_chinese_is_not_ascii_escaped(self):
        frame = encode_envelope({"message": "未注册命令"})
        self.assertIn("未注册命令", frame)  # ensure_ascii=False

    def test_frame_under_limit_is_accepted(self):
        envelope = {"payload": "x" * (MAX_FRAME_BYTES - 256)}
        frame = encode_envelope(envelope)
        self.assertLessEqual(len(frame.encode("utf-8")), MAX_FRAME_BYTES)

    def test_frame_over_2mib_is_rejected(self):
        envelope = {"payload": "x" * (MAX_FRAME_BYTES + 1)}
        with self.assertRaises(FrameTooLargeError) as ctx:
            encode_envelope(envelope)
        self.assertGreater(ctx.exception.size_bytes, MAX_FRAME_BYTES)


class ParseFrameTest(unittest.TestCase):
    """parse_frame：合法帧、畸形 JSON、缺字段、类型/版本校验。"""

    def test_parses_valid_request(self):
        self.assertEqual(parse_frame(encode_envelope(_request())), _request())

    def test_missing_session_id_still_parses(self):
        # session_id 缺失不属于畸形帧：由 SessionState.verify 丢弃并记 stderr（§4.3）
        line = json.dumps({k: v for k, v in _request().items() if k != "session_id"})
        envelope = parse_frame(line)
        self.assertNotIn("session_id", envelope)

    def test_invalid_json_is_malformed(self):
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame("not-json-at-all\n")
        self.assertEqual(ctx.exception.reason, "invalid_json")

    def test_empty_line_is_malformed(self):
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame("\n")
        self.assertEqual(ctx.exception.reason, "invalid_json")

    def test_non_object_json_is_malformed(self):
        for text in ("[1,2,3]", "42", '"str"', "null"):
            with self.assertRaises(MalformedFrameError) as ctx:
                parse_frame(text)
            self.assertEqual(ctx.exception.reason, "not_an_object")

    def test_missing_required_field_is_malformed(self):
        for field in ("protocol_version", "type", "id", "command"):
            broken = {k: v for k, v in _request().items() if k != field}
            with self.assertRaises(MalformedFrameError) as ctx:
                parse_frame(json.dumps(broken))
            self.assertEqual(ctx.exception.reason, "missing_field:" + field)

    def test_wrong_field_type_is_malformed(self):
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame(json.dumps(_request(id=123)))
        self.assertEqual(ctx.exception.reason, "bad_field_type:id")
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame(json.dumps(_request(command=["hello"])))
        self.assertEqual(ctx.exception.reason, "bad_field_type:command")

    def test_unsupported_protocol_version_is_malformed(self):
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame(json.dumps(_request(protocol_version=2)))
        self.assertEqual(ctx.exception.reason, "unsupported_protocol_version")

    def test_boolean_protocol_version_is_malformed(self):
        # bool 是 int 子类：true == 1，必须显式拒绝
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame(json.dumps(_request(protocol_version=True)))
        self.assertEqual(ctx.exception.reason, "unsupported_protocol_version")

    def test_event_missing_required_field_is_malformed(self):
        # §5.4：event 附加必填 seq/name/timestamp（缺失=畸形计数）
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame(json.dumps(_request(type="event")))
        self.assertEqual(ctx.exception.reason, "missing_field:seq")

    def test_unknown_type_is_malformed(self):
        with self.assertRaises(MalformedFrameError) as ctx:
            parse_frame(json.dumps(_request(type="broadcast")))
        self.assertEqual(ctx.exception.reason, "unsupported_type:broadcast")


class TransportSendTest(unittest.TestCase):
    """StdioTransport：单 writer 队列逐行写出；2MiB 拒绝发送并记 PROTOCOL_ERROR。"""

    def _run_writer(self, transport):
        transport.start()
        transport.close_stdout()
        self.assertTrue(transport.join_writer(timeout=5.0))

    def test_send_writes_single_compact_line(self):
        out, err = io.StringIO(), io.StringIO()
        transport = StdioTransport(stdout=out, stdin=io.StringIO(), stderr=err)
        self.assertTrue(transport.send({"a": 1, "b": "文本"}))
        self._run_writer(transport)
        self.assertEqual(out.getvalue(), '{"a":1,"b":"文本"}\n')
        self.assertEqual(err.getvalue(), "")

    def test_send_raw_appends_lf_and_preserves_order(self):
        out, err = io.StringIO(), io.StringIO()
        transport = StdioTransport(stdout=out, stdin=io.StringIO(), stderr=err)
        transport.start()
        self.assertTrue(transport.send_raw("garbage-line"))
        self.assertTrue(transport.send({"n": 2}))
        transport.close_stdout()
        self.assertTrue(transport.join_writer(timeout=5.0))
        self.assertEqual(out.getvalue(), "garbage-line\n{\"n\":2}\n")

    def test_send_rejects_over_2mib_and_logs_protocol_error(self):
        out, err = io.StringIO(), io.StringIO()
        transport = StdioTransport(stdout=out, stdin=io.StringIO(), stderr=err)
        transport.start()
        self.assertFalse(transport.send({"payload": "x" * (MAX_FRAME_BYTES + 1)}))
        transport.close_stdout()
        self.assertTrue(transport.join_writer(timeout=5.0))
        self.assertEqual(out.getvalue(), "")  # 超限帧不产出任何 stdout 输出
        self.assertIn("PROTOCOL_ERROR", err.getvalue())

    def test_send_after_close_returns_false(self):
        out, err = io.StringIO(), io.StringIO()
        transport = StdioTransport(stdout=out, stdin=io.StringIO(), stderr=err)
        transport.start()
        transport.close_stdout()
        self.assertTrue(transport.join_writer(timeout=5.0))
        self.assertFalse(transport.send({"late": True}))

    def test_read_line_returns_none_on_eof(self):
        transport = StdioTransport(stdout=io.StringIO(), stdin=io.StringIO(""), stderr=io.StringIO())
        self.assertIsNone(transport.read_line())

    def test_read_line_returns_content_then_eof(self):
        transport = StdioTransport(
            stdout=io.StringIO(), stdin=io.StringIO("line-1\nline-2\n"), stderr=io.StringIO()
        )
        self.assertEqual(transport.read_line(), "line-1\n")
        self.assertEqual(transport.read_line(), "line-2\n")
        self.assertIsNone(transport.read_line())


class StdioConfigureTest(unittest.TestCase):
    """configure_stdio（BUG-3）：stdio 显式固定 UTF-8，不依赖 PYTHONUTF8。"""

    class _FakeStream(io.StringIO):
        """带 reconfigure 记录的替身，模拟 TextIOWrapper。"""

        def __init__(self) -> None:
            super().__init__()
            self.reconfigure_calls = []

        def reconfigure(self, **kwargs):  # type: ignore[override]
            self.reconfigure_calls.append(kwargs)

    class _PlainStream(io.StringIO):
        """无 reconfigure 方法的替身，触发兜底分支。"""

    def test_reconfigures_all_three_streams_to_utf8(self):
        stdin, stdout, stderr = self._FakeStream(), self._FakeStream(), self._FakeStream()
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            configure_stdio()
        self.assertEqual(stdin.reconfigure_calls, [{"encoding": "utf-8"}])
        # stdout 协议通道：必须禁用 Windows \n→\r\n 翻译，保持 LF 分帧
        self.assertEqual(stdout.reconfigure_calls, [{"encoding": "utf-8", "newline": "\n"}])
        self.assertEqual(stderr.reconfigure_calls, [{"encoding": "utf-8"}])

    def test_stream_without_reconfigure_falls_back_without_raising(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", self._PlainStream()), \
                mock.patch.object(sys, "stdout", self._PlainStream()), \
                mock.patch.object(sys, "stderr", err):
            configure_stdio()  # 兜底路径不得抛异常阻断启动
        self.assertIn("reconfigure", err.getvalue())


if __name__ == "__main__":
    unittest.main()
