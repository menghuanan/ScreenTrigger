# -*- coding: utf-8 -*-
"""TASK-012 命令层单测（backend/scripts/commands.py）。

覆盖：5 条命令 data 形状（03 逐字）、参数白名单（未声明参数 → INVALID_PARAMS 定位）、
``StorageError`` → ``DispatcherError`` 映射（06 四字段）、``save_script`` 新建/更新判定
（只给其一 → 缺失伙伴字段，Q7）、分页范围（Q6 limit 1–500）、``validate_script`` 的
``ENGINE_UNAVAILABLE`` 映射（Q1：retryable=false、无 details）。
"""

from __future__ import annotations

import io
import itertools
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.script.validator import Diagnostic, ScriptValidator, engine_unavailable  # noqa: E402
from backend.scripts import ScriptService, ScriptSourceStore  # noqa: E402
from backend.scripts.commands import register_script_commands  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.targets import RecordingCatalogEmitter, Repository  # noqa: E402

SESSION_ID = "s-script-cmd"


def _param(name="retry", type_="number", required=False, default=3, description=""):
    return {"name": name, "type": type_, "required": required, "default": default,
            "description": description}


class _FakeValidator(ScriptValidator):
    def __init__(self, diagnostics):
        self._diagnostics = list(diagnostics)

    def compile(self, source, name="script"):
        return list(self._diagnostics)


class _EngineUnavailableValidator(ScriptValidator):
    def compile(self, source, name="script"):
        raise engine_unavailable()


class _Transport:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(self, envelope: Dict[str, Any]) -> bool:
        self.sent.append(envelope)
        return True

    def read_line(self):
        return None


class _Session:
    session_id = SESSION_ID


class _CommandFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-script-cmd-")
        self.repo = Repository(self.dir)
        self.emitter = RecordingCatalogEmitter()
        self.counter = itertools.count(1)
        self.svc = ScriptService(
            self.repo,
            ScriptSourceStore(self.repo.data_root),
            validator=_FakeValidator([]),
            emitter=self.emitter,
            clock=lambda: "2026-09-21T09:00:00Z",
            id_gen=lambda: "s-%d" % next(self.counter),
        )
        self.transport = _Transport()
        self.dispatcher = Dispatcher(
            transport=self.transport, session=_Session(), stderr=io.StringIO()
        )
        register_script_commands(self.dispatcher, self.svc)

    def _request(self, command: str, params: Dict[str, Any], request_id: str = "r1") -> Dict[str, Any]:
        envelope = {
            "protocol_version": 1,
            "session_id": SESSION_ID,
            "type": "request",
            "id": request_id,
            "command": command,
            "params": params,
        }
        self.dispatcher.dispatch_command(envelope)
        return self.transport.sent[-1]

    def _save(self, **over) -> Dict[str, Any]:
        params = {"name": "登录点击", "source": "return 1", "parameters": [_param()]}
        params.update(over)
        return self._request("save_script", params)


class CommandShapeTest(_CommandFixture):
    def test_save_script_shape(self):
        response = self._save()
        self.assertTrue(response["success"], response)
        script = response["data"]["script"]
        self.assertEqual(
            set(script.keys()),
            {"id", "revision", "name", "source_path", "source_sha256", "parameters",
             "created_at", "updated_at"},
        )
        self.assertEqual(script["revision"], 1)

    def test_save_script_update(self):
        created = self._save()["data"]["script"]
        updated = self._save(
            source="return 2", script_id=created["id"], expected_revision=created["revision"]
        )["data"]["script"]
        self.assertEqual(updated["revision"], 2)

    def test_save_script_only_id_reports_missing_partner(self):
        response = self._save(script_id="s-1")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "expected_revision")

    def test_save_script_only_revision_reports_missing_partner(self):
        response = self._save(expected_revision=1)
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["details"]["field"], "script_id")

    def test_load_script_shape_and_not_found(self):
        created = self._save()["data"]["script"]
        got = self._request("load_script", {"script_id": created["id"]}, "r2")
        self.assertTrue(got["success"], got)
        self.assertEqual(got["data"]["script"]["id"], created["id"])
        self.assertEqual(got["data"]["source"], "return 1")
        missing = self._request("load_script", {"script_id": "nope"}, "r3")
        self.assertFalse(missing["success"])
        self.assertEqual(missing["error"]["code"], "SCRIPT_NOT_FOUND")
        self.assertEqual(missing["error"]["details"]["field"], "script_id")

    def test_list_scripts_shape_and_defaults(self):
        self._save()
        response = self._request("list_scripts", {}, "l1")
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["total"], 1)
        self.assertEqual(len(response["data"]["items"]), 1)

    def test_list_scripts_limit_out_of_range(self):
        response = self._request("list_scripts", {"limit": 501}, "l1")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "limit")

    def test_delete_script_returns_ok(self):
        created = self._save()["data"]["script"]
        response = self._request(
            "delete_script", {"script_id": created["id"], "expected_revision": 1}, "d1"
        )
        self.assertTrue(response["success"], response)
        self.assertEqual(response["data"], {"ok": True})

    def test_undeclared_param_rejected_with_field(self):
        response = self._request("list_scripts", {"foo": 1}, "l1")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "foo")


class ValidateScriptCommandTest(_CommandFixture):
    def test_validate_script_returns_diagnostics(self):
        self.dispatcher = Dispatcher(
            transport=self.transport, session=_Session(), stderr=io.StringIO()
        )
        svc = ScriptService(
            self.repo,
            ScriptSourceStore(self.repo.data_root),
            validator=_FakeValidator([Diagnostic(line=2, column=None, message="boom")]),
        )
        register_script_commands(self.dispatcher, svc)
        response = self._request("validate_script", {"source": "local x ="})
        self.assertTrue(response["success"], response)
        self.assertFalse(response["data"]["valid"])
        self.assertEqual(
            response["data"]["diagnostics"],
            [{"line": 2, "column": None, "message": "boom"}],
        )

    def test_validate_script_invalid_source(self):
        response = self._request("validate_script", {"source": 123})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "source")

    def test_validate_script_engine_unavailable_mapping(self):
        self.dispatcher = Dispatcher(
            transport=self.transport, session=_Session(), stderr=io.StringIO()
        )
        svc = ScriptService(
            self.repo,
            ScriptSourceStore(self.repo.data_root),
            validator=_EngineUnavailableValidator(),
        )
        register_script_commands(self.dispatcher, svc)
        response = self._request("validate_script", {"source": "return 1"})
        self.assertFalse(response["success"])
        error = response["error"]
        self.assertEqual(error["code"], "ENGINE_UNAVAILABLE")
        self.assertIs(error["retryable"], False)
        self.assertEqual(error["details"], {})


class SaveScriptRevisionValidationTest(_CommandFixture):
    """F-2 回归：``save_script.expected_revision`` 非 int 必须 INVALID_PARAMS（不崩进程）。

    经真实 Dispatcher；断言响应存在（未逃逸击落），错误码/字段正确。与 ``delete_script``
    的 ``_require_int`` 口径一致（str/None/float/list/bool 均拒绝）。
    """

    def _assert_revision_invalid(self, value, request_id):
        response = self._request(
            "save_script",
            {"name": "n", "source": "return 1", "parameters": [_param()],
             "expected_revision": value},
            request_id,
        )
        self.assertFalse(response["success"], response)
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "expected_revision")

    def test_string_revision_rejected(self):
        self._assert_revision_invalid("abc", "e1")

    def test_none_revision_rejected(self):
        self._assert_revision_invalid(None, "e2")

    def test_float_revision_rejected(self):
        self._assert_revision_invalid(1.5, "e3")

    def test_bool_revision_rejected(self):
        self._assert_revision_invalid(True, "e4")

    def test_list_revision_rejected(self):
        self._assert_revision_invalid([1], "e5")

    def test_revision_with_valid_id_still_rejected(self):
        response = self._request(
            "save_script",
            {"name": "n", "source": "return 1", "parameters": [_param()],
             "script_id": "s-1", "expected_revision": "abc"},
            "e6",
        )
        self.assertFalse(response["success"], response)
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "expected_revision")

    def test_non_string_script_id_rejected(self):
        response = self._request(
            "save_script",
            {"name": "n", "source": "return 1", "parameters": [_param()],
             "script_id": 123, "expected_revision": 1},
            "e7",
        )
        self.assertFalse(response["success"], response)
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "script_id")


class ErrorStructureTest(_CommandFixture):
    def test_errors_are_four_field(self):
        response = self._request("load_script", {"script_id": "nope"})
        error = response["error"]
        self.assertEqual(set(error.keys()), {"code", "message", "details", "retryable"})
        self.assertIs(error["retryable"], False)


if __name__ == "__main__":
    unittest.main()
