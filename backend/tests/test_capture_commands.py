# -*- coding: utf-8 -*-
"""TASK-007 捕获命令接线单测（backend/capture/commands.py + main.py 装配）。

覆盖：6 条命令 data 形状（03 逐字）、未声明参数 INVALID_PARAMS 定位、类型/区域
错误 → DispatcherError 映射、release 幂等与过期拒绝、预览参数范围、捕获命令
**不产生任何 host_request / input_action**（捕获不调用 Input 的运行时断言），
以及 SidecarApp 装配捕获服务后 hello 能力据实置 capture=available。
"""

from __future__ import annotations

import ast
import base64
import io
import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.commands import register_capture_commands  # noqa: E402
from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.main import (  # noqa: E402
    CAPABILITIES,
    SidecarApp,
    SidecarArgs,
    StdioTransport,
)
from backend.tests._capture_fakes import (  # noqa: E402
    CaptureTransport,
    FakeCaptureBackend,
    FakeWindowProvider,
    SessionStub,
    display,
    make_service,
    window_snapshot,
)
from backend.types.region import Region  # noqa: E402


def _region_dict(space="screen_physical", x=0, y=0, width=100, height=50,
                 display_id="d1", binding_id=None):
    value = {"space": space, "x": x, "y": y, "width": width, "height": height}
    if space == "screen_physical":
        value["display_id"] = display_id
    elif space == "window_client_physical":
        value["binding_id"] = binding_id
    return value


class CommandWiringTest(unittest.TestCase):
    def setUp(self):
        self.backend = FakeCaptureBackend()
        self.provider = FakeWindowProvider({"b1": window_snapshot("b1", 300, 400, 500, 300, 7)})
        self.service = make_service(
            displays=[display("d1", 0, 0, 1920, 1080, primary=True)],
            backend=self.backend,
            window_provider=self.provider,
        )
        self.transport = CaptureTransport()
        self.dispatcher = Dispatcher(transport=self.transport, session=SessionStub())
        register_capture_commands(self.dispatcher, self.service)

    def _request(self, command, params, request_id="r1"):
        envelope = {
            "protocol_version": 1,
            "session_id": SessionStub.session_id,
            "type": "request",
            "id": request_id,
            "command": command,
            "params": params,
        }
        self.dispatcher.dispatch_command(envelope)
        return self.transport.sent[-1]

    def test_get_display_list(self):
        response = self._request("get_display_list", {})
        self.assertTrue(response["success"])
        displays = response["data"]["displays"]
        self.assertEqual(len(displays), 1)
        self.assertEqual(displays[0]["id"], "d1")
        self.assertEqual(displays[0]["rect"], {"x": 0, "y": 0, "width": 1920, "height": 1080})
        self.assertEqual(displays[0]["dpi_scale"], 1.0)
        self.assertTrue(displays[0]["primary"])

    def test_capture_region_shape(self):
        response = self._request("capture_region", {"region": _region_dict()})
        self.assertTrue(response["success"])
        data = response["data"]
        self.assertEqual(set(data.keys()), {"image", "frame_id"})
        image = data["image"]
        self.assertEqual(set(image.keys()), {"image_id", "width", "height", "format", "sha256"})
        self.assertEqual(image["width"], 100)
        self.assertEqual(image["height"], 50)
        self.assertEqual(image["format"], "png")
        self.assertRegex(image["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(data["frame_id"].startswith("frame-"))

    def test_capture_full_screen_and_window(self):
        full = self._request("capture_full_screen", {"display_id": "d1"}, "r2")
        self.assertTrue(full["success"])
        self.assertEqual(full["data"]["image"]["width"], 1920)
        win = self._request("capture_window", {"binding_id": "b1"}, "r3")
        self.assertTrue(win["success"])
        self.assertEqual(win["data"]["image"]["width"], 500)
        self.assertEqual(win["data"]["image"]["height"], 300)

    def test_capture_region_with_client_space(self):
        response = self._request(
            "capture_region",
            {"region": _region_dict(space="window_client_physical", x=10, y=20,
                                    width=100, height=50, binding_id="b1")},
        )
        self.assertTrue(response["success"])
        self.assertEqual(self.backend.calls[-1], (310, 420, 100, 50))

    # -- 参数/契约错误 ---------------------------------------------------------

    def test_undeclared_param_rejected_with_field(self):
        response = self._request("get_display_list", {"junk": 1})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "junk")

    def test_capture_region_missing_region(self):
        response = self._request("capture_region", {})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "region")

    def test_capture_region_min_edge_violation(self):
        response = self._request(
            "capture_region", {"region": _region_dict(width=9, height=10)}
        )
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_REGION")
        self.assertEqual(response["error"]["details"]["field"], "region.width")

    def test_capture_region_unknown_space(self):
        response = self._request(
            "capture_region",
            {"region": {"space": "virtual_screen", "x": 0, "y": 0, "width": 100, "height": 50}},
        )
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "region.space")

    def test_capture_full_screen_missing_display_id(self):
        response = self._request("capture_full_screen", {})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "display_id")

    def test_capture_full_screen_display_not_found(self):
        response = self._request("capture_full_screen", {"display_id": "gone"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "DISPLAY_NOT_FOUND")

    def test_capture_window_not_found(self):
        response = self._request("capture_window", {"binding_id": "missing"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "WINDOW_NOT_FOUND")

    # -- 图像引用 -------------------------------------------------------------

    def test_release_image_idempotent(self):
        captured = self._request("capture_region", {"region": _region_dict()}, "c1")
        image_id = captured["data"]["image"]["image_id"]
        first = self._request("release_image", {"image_id": image_id}, "rel1")
        self.assertEqual(first["data"], {"ok": True})
        second = self._request("release_image", {"image_id": image_id}, "rel2")
        self.assertEqual(second["data"], {"ok": True})  # 幂等

    def test_release_image_unknown_expired(self):
        response = self._request("release_image", {"image_id": "img-nope"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "IMAGE_EXPIRED")

    def test_get_image_preview(self):
        captured = self._request("capture_region", {"region": _region_dict()}, "c2")
        image_id = captured["data"]["image"]["image_id"]
        response = self._request(
            "get_image_preview", {"image_id": image_id, "max_edge": 100}, "p1"
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["mime"], "image/png")
        raw = base64.b64decode(response["data"]["base64"])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")

    def test_get_image_preview_default_max_edge(self):
        captured = self._request("capture_region", {"region": _region_dict()}, "c3")
        image_id = captured["data"]["image"]["image_id"]
        response = self._request("get_image_preview", {"image_id": image_id}, "p2")
        self.assertTrue(response["success"])  # max_edge 可省略（默认 400）

    def test_get_image_preview_max_edge_out_of_range(self):
        captured = self._request("capture_region", {"region": _region_dict()}, "c4")
        image_id = captured["data"]["image"]["image_id"]
        for bad in (0, 801):
            response = self._request(
                "get_image_preview", {"image_id": image_id, "max_edge": bad}, "p3"
            )
            self.assertFalse(response["success"])
            self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
            self.assertEqual(response["error"]["details"]["field"], "max_edge")

    # -- 捕获不调用 Input -------------------------------------------------------

    def test_capture_commands_emit_no_input_or_host_request(self):
        self._request("capture_region", {"region": _region_dict()}, "n1")
        self._request("capture_full_screen", {"display_id": "d1"}, "n2")
        self._request("capture_window", {"binding_id": "b1"}, "n3")
        for envelope in self.transport.sent:
            self.assertEqual(envelope["type"], "response")
            self.assertNotIn("input_action", json.dumps(envelope))


class NoInputSourceTest(unittest.TestCase):
    """静态断言：捕获/图像包源码不调用任何输入派发 API（捕获不调用 Input）。

    以 AST 遍历标识符/属性名，避免把 docstring 里对「不调用 SendInput」的说明
    误判为调用；只拦截真实的 Win32 输入符号与宿主输入命令名。
    """

    FORBIDDEN_NAMES = (
        "SendInput", "keybd_event", "mouse_event", "SetCursorPos",
        "input_action", "acquire_input_lease", "release_input_lease",
    )

    def test_capture_and_image_sources_have_no_input_calls(self):
        targets = []
        for pkg in ("backend/capture", "backend/image"):
            root = os.path.join(_REPO_ROOT, *pkg.split("/"))
            for name in sorted(os.listdir(root)):
                if name.endswith(".py"):
                    targets.append(os.path.join(root, name))
        self.assertTrue(targets, "未找到捕获/图像源码文件")
        for path in targets:
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    used.add(node.attr)
            for token in self.FORBIDDEN_NAMES:
                self.assertNotIn(token, used,
                                 "捕获/图像源码不得调用输入 API：%s（%s）" % (token, path))


class CapabilityWiringTest(unittest.TestCase):
    """装配捕获服务后 hello/system_ready 的 capture 能力据实置 available。"""

    def _make_app(self, capture_service):
        out, err, stdin = io.StringIO(), io.StringIO(), io.StringIO("")
        transport = StdioTransport(stdout=out, stdin=stdin, stderr=err)
        args = SidecarArgs(session_id="s-test", fault_scenario=None)
        app = SidecarApp(
            args=args, transport=transport, stderr=err,
            lua_probe=lambda: "Lua 5.4", capture_service=capture_service,
        )
        return app

    def test_without_capture_service_stays_unavailable(self):
        app = self._make_app(None)
        caps = app.capabilities()
        capture = [c for c in caps if c["id"] == "capture"][0]
        self.assertEqual(capture["state"], "unavailable")
        self.assertEqual([dict(c) for c in caps], [dict(c) for c in CAPABILITIES])

    def test_with_capture_service_marked_available(self):
        service = make_service(displays=[display("d1", 0, 0, 800, 600, primary=True)])
        app = self._make_app(service)
        capture = [c for c in app.capabilities() if c["id"] == "capture"][0]
        self.assertEqual(capture["state"], "available")
        self.assertIsNone(capture["reason"])
        # 其余能力保持骨架 unavailable（不误报）
        others = [c for c in app.capabilities() if c["id"] != "capture"]
        self.assertTrue(all(c["state"] == "unavailable" for c in others))
        # hello 数据同样反映可用
        hello = app.cmd_hello()
        capture_hello = [c for c in hello["capabilities"] if c["id"] == "capture"][0]
        self.assertEqual(capture_hello["state"], "available")


if __name__ == "__main__":
    unittest.main()
