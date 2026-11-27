# -*- coding: utf-8 -*-
"""TASK-011 命令层单测（backend/targets/commands.py）。

覆盖：10 条命令 data 形状（03 逐字）、参数白名单（未声明参数 → INVALID_PARAMS 定位）、
``StorageError`` → ``DispatcherError`` 映射（06 四字段）、``update_group`` 键缺省/
显式 null 语义、分页范围校验。
"""

from __future__ import annotations

import base64
import io
import itertools
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.ports import RawImage  # noqa: E402
from backend.image.store import SessionImageStore  # noqa: E402
from backend.image.png_decode import decode_png_to_rgb
from backend.ipc.dispatcher import Dispatcher  # noqa: E402
from backend.storage import DataRoot  # noqa: E402
from backend.storage.imagestore import ImageStore  # noqa: E402
from backend.targets import RecordingCatalogEmitter, Repository, TargetService  # noqa: E402
from backend.targets.commands import register_target_commands  # noqa: E402

_TS = "2026-09-21T09:00:00Z"
SESSION_ID = "s-cmd"


def _cond() -> dict:
    return {"op": "all", "children": [{"id": "c1", "type": "template", "params": {"threshold": 0.85}}]}


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
        self.dir = tempfile.mkdtemp(prefix="st-cmd-")
        self.session = SessionImageStore(session_id=SESSION_ID)
        self.asset_store = ImageStore(self.session, DataRoot(self.dir))
        self.repo = Repository(self.dir, image_store=self.asset_store)
        self.emitter = RecordingCatalogEmitter()
        counter = itertools.count(1)
        self.svc = TargetService(
            self.repo, self.emitter, clock=lambda: _TS, id_gen=lambda: "id-%d" % next(counter)
        )
        self.transport = _Transport()
        self.dispatcher = Dispatcher(
            transport=self.transport, session=_Session(), stderr=io.StringIO()
        )
        register_target_commands(self.dispatcher, self.svc)
        self.ref = self.session.put(RawImage(width=4, height=4, data=bytes([9, 8, 7]) * 16), "png")

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

    def _draft(self) -> dict:
        return {
            "name": "开始按钮",
            "image_id": self.ref.image_id,
            "anchor_condition_id": "c1",
            "conditions": _cond(),
        }


class CommandShapeTest(_CommandFixture):
    def test_create_target_shape(self):
        response = self._request("create_target", {"draft": self._draft()})
        self.assertTrue(response["success"])
        target = response["data"]["target"]
        self.assertEqual(
            set(target.keys()),
            {
                "id", "sample_no", "revision", "name", "group_id", "image",
                "thumbnail_path", "anchor_condition_id", "conditions", "created_at",
                "updated_at", "metadata",
            },
        )
        self.assertEqual(target["sample_no"], 1)

    def test_get_target_shape_and_not_found(self):
        created = self._request("create_target", {"draft": self._draft()})["data"]["target"]
        got = self._request("get_target", {"target_id": created["id"]}, "r2")
        self.assertTrue(got["success"])
        self.assertEqual(got["data"]["target"]["id"], created["id"])
        missing = self._request("get_target", {"target_id": "nope"}, "r3")
        self.assertFalse(missing["success"])
        self.assertEqual(missing["error"]["code"], "TARGET_NOT_FOUND")
        self.assertEqual(missing["error"]["details"]["field"], "target_id")

    def test_saved_preview_after_session_image_is_released(self):
        target = self._request("create_target", {"draft": self._draft()})["data"]["target"]
        self.session.release(self.ref.image_id)
        params = {"target_id": target["id"]}
        self.assertNotIn("preview", self._request("get_target", params, "plain")["data"])
        response = self._request("get_target", dict(params, preview_max_edge=2), "preview")
        self.assertEqual(response["data"]["target"]["revision"], target["revision"])
        image = response["data"]["preview"]
        self.assertEqual(image["mime"], "image/png")
        self.assertEqual(decode_png_to_rgb(base64.b64decode(image["base64"])), (2, 2, bytes([7, 8, 9]) * 4))
        for invalid in (0, 801, True):
            with self.subTest(edge=invalid):
                bad = self._request("get_target", dict(params, preview_max_edge=invalid), f"bad-{invalid}")
                self.assertEqual(bad["error"]["details"]["field"], "preview_max_edge")

    def test_list_targets_pagination_and_group_filter(self):
        group = self._request("create_group", {"name": "A"}, "g1")["data"]["group"]
        self._request(
            "create_target", {"draft": dict(self._draft(), group_id=group["id"])}, "t1"
        )
        self._request("create_target", {"draft": self._draft()}, "t2")
        all_items = self._request("list_targets", {}, "l1")
        self.assertEqual(all_items["data"]["total"], 2)
        grouped = self._request("list_targets", {"group_id": group["id"]}, "l2")
        self.assertEqual(grouped["data"]["total"], 1)
        ungroupped = self._request("list_targets", {"group_id": None}, "l3")
        self.assertEqual(ungroupped["data"]["total"], 1)

    def test_list_targets_limit_out_of_range(self):
        response = self._request("list_targets", {"limit": 501}, "l1")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "limit")

    def test_delete_target_returns_ok(self):
        created = self._request("create_target", {"draft": self._draft()})["data"]["target"]
        response = self._request(
            "delete_target", {"target_id": created["id"], "expected_revision": 1}, "d1"
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["data"], {"ok": True})

    def test_update_target_forbidden_key(self):
        created = self._request("create_target", {"draft": self._draft()})["data"]["target"]
        response = self._request(
            "update_target",
            {"target_id": created["id"], "expected_revision": 1, "patch": {"sample_no": 9}},
            "u1",
        )
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "sample_no")

    def test_move_target_to_group_shape(self):
        group = self._request("create_group", {"name": "A"}, "g1")["data"]["group"]
        created = self._request("create_target", {"draft": self._draft()})["data"]["target"]
        response = self._request(
            "move_target_to_group",
            {"target_id": created["id"], "group_id": group["id"], "expected_revision": 1},
            "m1",
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["target"]["group_id"], group["id"])


class GroupCommandTest(_CommandFixture):
    def test_create_group_shape(self):
        response = self._request("create_group", {"name": "组一"})
        self.assertTrue(response["success"])
        group = response["data"]["group"]
        self.assertEqual(
            set(group.keys()),
            {"id", "revision", "name", "parent_group_id", "depth", "created_at", "updated_at"},
        )
        self.assertEqual(group["depth"], 0)

    def test_list_groups_includes_derived_target_count(self):
        self._request("create_group", {"name": "A"}, "g1")
        response = self._request("list_groups", {}, "l1")
        self.assertTrue(response["success"])
        self.assertEqual(len(response["data"]["groups"]), 1)
        self.assertIn("target_count", response["data"]["groups"][0])

    def test_create_group_parent_not_found(self):
        response = self._request("create_group", {"name": "X", "parent_group_id": "g-x"})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "GROUP_NOT_FOUND")
        self.assertEqual(response["error"]["details"]["field"], "parent_group_id")

    def test_update_group_requires_a_field(self):
        group = self._request("create_group", {"name": "A"}, "g1")["data"]["group"]
        response = self._request(
            "update_group", {"group_id": group["id"], "expected_revision": 1}, "u1"
        )
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "name")

    def test_update_group_explicit_null_moves_to_top(self):
        parent = self._request("create_group", {"name": "P"}, "g1")["data"]["group"]
        child = self._request(
            "create_group", {"name": "C", "parent_group_id": parent["id"]}, "g2"
        )["data"]["group"]
        self.assertEqual(child["depth"], 1)
        response = self._request(
            "update_group",
            {"group_id": child["id"], "parent_group_id": None, "expected_revision": child["revision"]},
            "u1",
        )
        self.assertTrue(response["success"])
        self.assertIsNone(response["data"]["group"]["parent_group_id"])
        self.assertEqual(response["data"]["group"]["depth"], 0)

    def test_update_group_name_absent_but_parent_present_ok(self):
        parent = self._request("create_group", {"name": "P"}, "g1")["data"]["group"]
        child = self._request(
            "create_group", {"name": "C", "parent_group_id": parent["id"]}, "g2"
        )["data"]["group"]
        response = self._request(
            "update_group",
            {"group_id": child["id"], "parent_group_id": None, "expected_revision": 1},
            "u1",
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["group"]["name"], "C")  # 未给 name → 不改名

    def test_delete_group_returns_ok(self):
        group = self._request("create_group", {"name": "A"}, "g1")["data"]["group"]
        response = self._request(
            "delete_group", {"group_id": group["id"], "expected_revision": 1}, "d1"
        )
        self.assertTrue(response["success"])
        self.assertEqual(response["data"], {"ok": True})

    def test_undeclared_param_rejected_with_field(self):
        response = self._request("create_group", {"name": "A", "depth": 0})
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(response["error"]["details"]["field"], "depth")

    def test_expected_revision_required(self):
        group = self._request("create_group", {"name": "A"}, "g1")["data"]["group"]
        response = self._request("update_group", {"group_id": group["id"], "name": "B"}, "u1")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["details"]["field"], "expected_revision")


class ErrorStructureTest(_CommandFixture):
    def test_errors_are_four_field(self):
        response = self._request("get_target", {"target_id": "nope"})
        error = response["error"]
        self.assertEqual(set(error.keys()), {"code", "message", "details", "retryable"})
        self.assertIs(error["retryable"], False)


class ResourceLimitFieldTest(_CommandFixture):
    """F3 修复：数量超限 RESOURCE_LIMIT 必须带 details.field（targets / groups）。

    经**命令层**触发（非直接调 storage）；阈值从 ``backend.targets.domain`` 导入的
    ``MAX_TARGETS`` / ``MAX_GROUPS`` 打桩调小，避免造 1 万实体。
    """

    def test_target_limit_reports_field_targets(self):
        with mock.patch("backend.targets.domain.MAX_TARGETS", 2):
            self._request("create_target", {"draft": self._draft()}, "t1")
            self._request("create_target", {"draft": self._draft()}, "t2")
            response = self._request("create_target", {"draft": self._draft()}, "t3")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "RESOURCE_LIMIT")
        self.assertEqual(response["error"]["details"].get("field"), "targets")

    def test_group_limit_reports_field_groups(self):
        with mock.patch("backend.targets.domain.MAX_GROUPS", 1):
            self._request("create_group", {"name": "A"}, "g1")
            response = self._request("create_group", {"name": "B"}, "g2")
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "RESOURCE_LIMIT")
        self.assertEqual(response["error"]["details"].get("field"), "groups")


if __name__ == "__main__":
    unittest.main()
