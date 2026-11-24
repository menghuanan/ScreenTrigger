# -*- coding: utf-8 -*-
"""输入窄代理契约测试（TASK-017 / TEST-016 host_request 子集）。

覆盖：
- 命令与参数形状（03 逐字）：`input_action` / `acquire_input_lease` / `release_input_lease`；
- 成功解包（`dispatched`/`lease_id`）与失败映射（06 四字段 → ``InputProxyError``）；
- CAPABILITY_DENIED 原样透出；
- **不自动重试**（失败后 host 调用次数恒 1）、**不 clamp、不校验几何**（代理不改变坐标）；
- 便利封装构造的 action 形状。
"""

from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.input_proxy import InputProxy, InputProxyError  # noqa: E402
from backend.ipc.hostclient import HostCallResult  # noqa: E402


class FakeHostClient:
    """记录调用并按命令返回预设结果的 HostClient 替身。"""

    def __init__(self, results=None):
        self.calls = []
        self._results = results if results is not None else {}

    def request(self, command, params, timeout_s=5.0):
        self.calls.append((command, params))
        spec = self._results.get(command, {"success": True, "data": {}})
        if isinstance(spec, HostCallResult):
            return spec
        return HostCallResult(
            bool(spec.get("success", True)),
            data=spec.get("data"),
            error=spec.get("error"),
        )


def _error(code, retryable=False):
    return {"code": code, "message": "boom", "details": {}, "retryable": retryable}


class AcquireLeaseTest(unittest.TestCase):
    def test_acquire_sends_execution_and_context(self):
        host = FakeHostClient({"acquire_input_lease": {"success": True, "data": {"lease_id": "lease-1"}}})
        proxy = InputProxy(host)
        lease = proxy.acquire_lease("exec-1", {"allow_exit": True})
        self.assertEqual(lease, "lease-1")
        self.assertEqual(host.calls, [("acquire_input_lease", {"execution_id": "exec-1", "context": {"allow_exit": True}})])

    def test_acquire_without_context_omits_key(self):
        host = FakeHostClient({"acquire_input_lease": {"success": True, "data": {"lease_id": "lease-2"}}})
        proxy = InputProxy(host)
        proxy.acquire_lease("exec-1")
        self.assertEqual(host.calls[0][1], {"execution_id": "exec-1"})

    def test_busy_error_surfaces_retryable(self):
        host = FakeHostClient({"acquire_input_lease": {"success": False, "error": _error("BUSY", True)}})
        proxy = InputProxy(host)
        with self.assertRaises(InputProxyError) as ctx:
            proxy.acquire_lease("exec-1")
        self.assertEqual(ctx.exception.code, "BUSY")
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(host.calls), 1, "代理不得自动重试")

    def test_missing_lease_id_is_internal_error(self):
        host = FakeHostClient({"acquire_input_lease": {"success": True, "data": {}}})
        proxy = InputProxy(host)
        with self.assertRaises(InputProxyError) as ctx:
            proxy.acquire_lease("exec-1")
        self.assertEqual(ctx.exception.code, "INTERNAL_ERROR")


class DispatchActionTest(unittest.TestCase):
    def test_dispatch_sends_full_params(self):
        host = FakeHostClient({"input_action": {"success": True, "data": {"action_id": "a1", "dispatched": True}}})
        proxy = InputProxy(host)
        ok = proxy.dispatch_action("exec-1", "lease-9", "a1", 3, {"kind": "move", "x": 10, "y": 20, "duration_ms": 0})
        self.assertTrue(ok)
        command, params = host.calls[0]
        self.assertEqual(command, "input_action")
        self.assertEqual(
            params,
            {
                "execution_id": "exec-1",
                "lease_id": "lease-9",
                "action_id": "a1",
                "geometry_revision": 3,
                "action": {"kind": "move", "x": 10, "y": 20, "duration_ms": 0},
            },
        )

    def test_out_of_region_error_is_not_clamped(self):
        # 代理绝不改动坐标：越界由 Rust 拒绝并回 REGION_LIMIT_VIOLATION，原样抛出。
        host = FakeHostClient({"input_action": {"success": False, "error": _error("REGION_LIMIT_VIOLATION")}})
        proxy = InputProxy(host)
        with self.assertRaises(InputProxyError) as ctx:
            proxy.dispatch_action("exec-1", "l1", "a1", 0, {"kind": "move", "x": 999999, "y": -50, "duration_ms": 0})
        self.assertEqual(ctx.exception.code, "REGION_LIMIT_VIOLATION")
        # 坐标原样传给宿主（未 clamp、未重试）
        self.assertEqual(host.calls[0][1]["action"]["x"], 999999)
        self.assertEqual(len(host.calls), 1)

    def test_stale_frame_carries_retryable(self):
        host = FakeHostClient({"input_action": {"success": False, "error": _error("STALE_FRAME", True)}})
        proxy = InputProxy(host)
        with self.assertRaises(InputProxyError) as ctx:
            proxy.dispatch_action("exec-1", "l1", "a1", 5, {"kind": "click", "x": 1, "y": 1})
        self.assertEqual(ctx.exception.code, "STALE_FRAME")
        self.assertTrue(ctx.exception.retryable)

    def test_capability_denied_passthrough(self):
        host = FakeHostClient({"input_action": {"success": False, "error": _error("CAPABILITY_DENIED")}})
        proxy = InputProxy(host)
        with self.assertRaises(InputProxyError) as ctx:
            proxy.dispatch_action("exec-1", "l1", "a1", 0, {"kind": "scroll", "delta": 1})
        self.assertEqual(ctx.exception.code, "CAPABILITY_DENIED")


class ReleaseLeaseTest(unittest.TestCase):
    def test_release_calls_command(self):
        host = FakeHostClient({"release_input_lease": {"success": True, "data": {"ok": True}}})
        proxy = InputProxy(host)
        proxy.release_lease("exec-1")
        self.assertEqual(host.calls, [("release_input_lease", {"execution_id": "exec-1"})])


class ConvenienceTest(unittest.TestCase):
    def setUp(self):
        self.host = FakeHostClient({"input_action": {"success": True, "data": {"dispatched": True}}})
        self.proxy = InputProxy(self.host)

    def _action(self):
        return self.host.calls[0][1]["action"]

    def test_move_shape(self):
        self.proxy.move("e", "l", "a", 5, 6, duration_ms=100)
        self.assertEqual(self._action(), {"kind": "move", "x": 5, "y": 6, "duration_ms": 100})

    def test_click_shape(self):
        self.proxy.click("e", "l", "a", 5, 6, button="right", clicks=2)
        self.assertEqual(self._action(), {"kind": "click", "x": 5, "y": 6, "button": "right", "clicks": 2})

    def test_text_shape(self):
        self.proxy.type_text("e", "l", "a", "中文😀")
        self.assertEqual(self._action(), {"kind": "text", "text": "中文😀", "interval_ms": 0})

    def test_hotkey_shape(self):
        self.proxy.hotkey("e", "l", "a", ["ctrl", "a"])
        self.assertEqual(self._action(), {"kind": "hotkey", "keys": ["ctrl", "a"]})

    def test_window_snapshot_transcall(self):
        host = FakeHostClient({"window_snapshot": {"success": True, "data": {"binding": {"binding_id": "b1"}}}})
        proxy = InputProxy(host)
        data = proxy.window_snapshot("b1")
        self.assertEqual(host.calls, [("window_snapshot", {"binding_id": "b1"})])
        self.assertEqual(data["binding"]["binding_id"], "b1")


if __name__ == "__main__":
    unittest.main()
