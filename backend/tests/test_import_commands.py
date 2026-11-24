# -*- coding: utf-8 -*-
"""TASK-009：import_image 命令接线测试（与 capture 共享 SessionImageStore）。"""

from __future__ import annotations

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
from backend.image.commands import register_image_commands  # noqa: E402
from backend.image.import_service import ImageImportService  # noqa: E402
from backend.image.png import encode_png_bgr  # noqa: E402
from backend.image.store import SessionImageStore  # noqa: E402
from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.tests._capture_fakes import (  # noqa: E402
    CaptureTransport,
    SessionStub,
    display,
    make_service,
)
from backend.tests._import_fakes import (  # noqa: E402
    FakeFileReader,
    FakeTokenAuthority,
    corrupt_crc_png,
)


class ImportCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionImageStore(session_id=SessionStub.session_id)
        self.cap = make_service(
            displays=[display("d1", 0, 0, 800, 600, primary=True)], store=self.store
        )
        png = encode_png_bgr(bytes(4 * 3 * 3), 4, 3)
        self.authority = FakeTokenAuthority({"good": "C:/pick/a.png", "bad": "C:/pick/bad.png"})
        self.reader = FakeFileReader({"C:/pick/a.png": png, "C:/pick/bad.png": corrupt_crc_png()})
        self.import_service = ImageImportService(
            session_id=SessionStub.session_id,
            store=self.store,
            token_authority=self.authority,
            file_reader=self.reader,
        )
        self.transport = CaptureTransport()
        self.dispatcher = Dispatcher(transport=self.transport, session=SessionStub())
        register_capture_commands(self.dispatcher, self.cap)
        register_image_commands(self.dispatcher, self.import_service)

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

    def test_import_image_shape(self) -> None:
        response = self._request("import_image", {"import_token": "good"})
        self.assertTrue(response["success"])
        data = response["data"]
        self.assertEqual(set(data.keys()), {"image"})
        image = data["image"]
        self.assertEqual(set(image.keys()), {"image_id", "width", "height", "format", "sha256"})
        self.assertEqual((image["width"], image["height"]), (4, 3))
        self.assertEqual(image["format"], "png")
        self.assertRegex(image["sha256"], r"^[0-9a-f]{64}$")

    def test_undeclared_param(self) -> None:
        response = self._request("import_image", {"import_token": "good", "path": "C:/evil"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "path")

    def test_missing_token(self) -> None:
        response = self._request("import_image", {})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "import_token")

    def test_unknown_token(self) -> None:
        response = self._request("import_image", {"import_token": "nope"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "IMAGE_EXPIRED")

    def test_corrupt_image(self) -> None:
        response = self._request("import_image", {"import_token": "bad"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_IMAGE")

    def test_imported_image_releasable_and_previewable(self) -> None:
        # 复用 capture 注册的 release_image / get_image_preview（共享 store）
        imported = self._request("import_image", {"import_token": "good"}, "i1")
        image_id = imported["data"]["image"]["image_id"]
        preview = self._request("get_image_preview", {"image_id": image_id}, "p1")
        self.assertTrue(preview["success"])
        self.assertEqual(preview["data"]["mime"], "image/png")
        raw = base64.b64decode(preview["data"]["base64"])
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        rel = self._request("release_image", {"image_id": image_id}, "rel1")
        self.assertEqual(rel["data"], {"ok": True})

    def test_no_host_request_or_input(self) -> None:
        self._request("import_image", {"import_token": "good"}, "n1")
        for envelope in self.transport.sent:
            self.assertEqual(envelope["type"], "response")
            self.assertNotIn("input_action", json.dumps(envelope))


class ImportAndCaptureShareStoreTest(unittest.TestCase):
    def test_release_across_sources(self) -> None:
        store = SessionImageStore(session_id=SessionStub.session_id)
        # 捕获服务与导入服务共用同一 store
        make_service(displays=[display("d1", 0, 0, 100, 100)], store=store)
        png = encode_png_bgr(bytes(2 * 2 * 3), 2, 2)
        imp = ImageImportService(
            session_id=SessionStub.session_id,
            store=store,
            token_authority=FakeTokenAuthority({"t": "C:/p/a.png"}),
            file_reader=FakeFileReader({"C:/p/a.png": png}),
        )
        ref, _asset = imp.import_image("t")
        # 捕获侧 store 与导入侧同一：release 对导入引用生效
        self.assertTrue(store.release(ref.image_id))


if __name__ == "__main__":
    unittest.main()
