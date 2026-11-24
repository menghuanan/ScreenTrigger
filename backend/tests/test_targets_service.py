# -*- coding: utf-8 -*-
"""TASK-011 服务层单测（backend/targets/service.py ``TargetService``）。

覆盖设计 §11 TEST-003 策略：新建/改名/移组/删组/重启、编号不复用、冲突不丢修改、
空条件/失效分组拒绝、活动引用不可删、三层分组/同层同名、``catalog_changed`` 事件；
并含 **跨模块集成用例**：捕获/导入签发的 ``image_id`` 经共享会话存储被目标服务
成功转为 ``ImageAsset``（资产落到 ``images/<sha256>.png``），证明 §3 的装配修正。
"""

from __future__ import annotations

import hashlib
import itertools
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.capture.ports import RawImage  # noqa: E402
from backend.image.png import encode_png_bgr  # noqa: E402
from backend.image.store import SessionImageStore  # noqa: E402
from backend.storage import DataRoot  # noqa: E402
from backend.storage.catalog import MAX_GROUPS, MAX_TARGETS  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.storage.imagestore import AssetWrite, ImageStore  # noqa: E402
from backend.targets import (  # noqa: E402
    CatalogChange,
    RecordingCatalogEmitter,
    Repository,
    TargetService,
)
from backend.types.region import validate_image_asset  # noqa: E402

_TS = "2026-09-21T09:00:00Z"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _cond(anchor: str = "c1") -> dict:
    return {"op": "all", "children": [{"id": anchor, "type": "template", "params": {"threshold": 0.85}}]}


def _code(ctx) -> str:
    return getattr(ctx.exception, "code", None)


class _ServiceFixture(unittest.TestCase):
    """共享装配：临时数据根 + 共享会话图像存储 + 记录型 emitter + 确定性 id_gen。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-svc-")
        self.session = SessionImageStore(session_id="s-svc")
        self.asset_store = ImageStore(self.session, DataRoot(self.dir))
        self.repo = Repository(self.dir, image_store=self.asset_store)
        self.emitter = RecordingCatalogEmitter()
        self.counter = itertools.count(1)
        self.svc = TargetService(
            self.repo,
            self.emitter,
            clock=lambda: _TS,
            id_gen=lambda: "id-%d" % next(self.counter),
        )
        self.ref = self.session.put(RawImage(width=4, height=4, data=bytes([10, 20, 30]) * 16), "png")

    def _draft(self, **over) -> dict:
        base = {
            "name": "开始按钮",
            "image_id": self.ref.image_id,
            "anchor_condition_id": "c1",
            "conditions": _cond(),
        }
        base.update(over)
        return base

    def _create(self, **over) -> dict:
        return self.svc.create_target(self._draft(**over))


class CreateTargetTest(_ServiceFixture):
    def test_create_assigns_stable_id_sample_and_revision(self):
        target = self._create()
        self.assertEqual(target["sample_no"], 1)
        self.assertEqual(target["revision"], 1)
        self.assertEqual(target["id"], "id-1")
        self.assertEqual(target["metadata"], {})
        self.assertIsNone(target["group_id"])
        # 资产落盘且为真实 PNG
        path = os.path.join(self.dir, target["image"]["path"])
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(8), _PNG_SIG)
        # 事件仅一次
        self.assertEqual(len(self.emitter.events), 1)
        self.assertEqual(self.emitter.events[0]["revision"], 1)

    def test_reload_after_restart(self):
        target = self._create()
        fresh = Repository(self.dir, image_store=self.asset_store)
        snapshot = fresh.read_snapshot()
        self.assertEqual(snapshot.target_by_id(target["id"])["sample_no"], 1)

    def test_empty_conditions_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self._create(conditions={"op": "all", "children": []})
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(self.emitter.events, [])

    def test_missing_group_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self._create(group_id="g-missing")
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")

    def test_image_expired_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self._create(image_id="img-not-in-session")
        self.assertEqual(_code(ctx), "IMAGE_EXPIRED")
        self.assertEqual(self.emitter.events, [])

    def test_sample_no_monotonic_no_reuse(self):
        first = self._create()
        self.svc.delete_target(first["id"], first["revision"])
        second = self._create()
        self.assertEqual(second["sample_no"], 2)  # 删除不回退


class UpdateMoveDeleteTest(_ServiceFixture):
    def test_rename_bumps_revision_keeps_id_and_sample(self):
        target = self._create()
        updated = self.svc.update_target(target["id"], {"name": "新名"}, target["revision"])
        self.assertEqual(updated["name"], "新名")
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["id"], target["id"])
        self.assertEqual(updated["sample_no"], target["sample_no"])

    def test_conflict_keeps_snapshot_and_no_event(self):
        target = self._create()
        self.emitter.events.clear()
        with self.assertRaises(StorageError) as ctx:
            self.svc.update_target(target["id"], {"name": "新名"}, 99)
        self.assertEqual(_code(ctx), "CONFLICT")
        self.assertEqual(self.emitter.events, [])
        still = self.svc.get_target(target["id"])
        self.assertEqual(still["name"], "开始按钮")  # 旧版本保留
        self.assertEqual(still["revision"], 1)

    def test_move_target_to_group_and_ungroup(self):
        group = self.svc.create_group("组一")
        target = self._create()
        moved = self.svc.move_target_to_group(target["id"], group["id"], target["revision"])
        self.assertEqual(moved["group_id"], group["id"])
        self.assertEqual(moved["revision"], 2)
        ungrouped = self.svc.move_target_to_group(target["id"], None, moved["revision"])
        self.assertIsNone(ungrouped["group_id"])

    def test_move_to_missing_group(self):
        target = self._create()
        with self.assertRaises(StorageError) as ctx:
            self.svc.move_target_to_group(target["id"], "g-x", target["revision"])
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")

    def test_delete_referenced_target_in_use(self):
        ref_repo = Repository(
            tempfile.mkdtemp(prefix="st-svc-ref-"),
            image_store=self.asset_store,
            reference_port=lambda eid: eid == "id-1",
        )
        svc = TargetService(ref_repo, RecordingCatalogEmitter(), clock=lambda: _TS,
                            id_gen=lambda: "id-1")
        target = svc.create_target(self._draft())
        with self.assertRaises(StorageError) as ctx:
            svc.delete_target(target["id"], target["revision"])
        self.assertEqual(_code(ctx), "IN_USE")


class GroupCrudTest(_ServiceFixture):
    def test_create_group_top_level(self):
        group = self.svc.create_group("组一")
        self.assertEqual(group["depth"], 0)
        self.assertIsNone(group["parent_group_id"])
        self.assertEqual(group["revision"], 1)

    def test_group_depth_three_levels_ok_fourth_rejected(self):
        a = self.svc.create_group("A")
        b = self.svc.create_group("B", a["id"])
        c = self.svc.create_group("C", b["id"])
        self.assertEqual(c["depth"], 2)
        with self.assertRaises(StorageError) as ctx:
            self.svc.create_group("D", c["id"])
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")
        self.assertEqual(ctx.exception.details.get("field"), "parent_group_id")

    def test_group_sibling_name_unique_nfc_casefold(self):
        self.svc.create_group("Café")
        with self.assertRaises(StorageError) as ctx:
            self.svc.create_group("CAFÉ")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "name")

    def test_group_parent_not_found(self):
        with self.assertRaises(StorageError) as ctx:
            self.svc.create_group("X", "g-missing")
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")
        self.assertEqual(ctx.exception.details.get("field"), "parent_group_id")

    def test_list_groups_derives_target_count(self):
        a = self.svc.create_group("A")
        b = self.svc.create_group("B")
        target = self._create(group_id=a["id"])
        groups = {g["id"]: g for g in self.svc.list_groups()}
        self.assertEqual(groups[a["id"]]["target_count"], 1)
        self.assertEqual(groups[b["id"]]["target_count"], 0)
        self.assertEqual(len(self.svc.list_groups()), 2)

    def test_update_group_requires_a_field(self):
        a = self.svc.create_group("A")
        with self.assertRaises(StorageError) as ctx:
            self.svc.update_group(a["id"], a["revision"])
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(ctx.exception.details.get("field"), "name")

    def test_update_group_rename(self):
        a = self.svc.create_group("A")
        updated = self.svc.update_group(a["id"], a["revision"], name="A2")
        self.assertEqual(updated["name"], "A2")
        self.assertEqual(updated["revision"], 2)

    def test_update_group_reparent_recomputes_depth(self):
        a = self.svc.create_group("A")
        b = self.svc.create_group("B", a["id"])
        moved = self.svc.update_group(b["id"], b["revision"], parent_group_id=None)
        self.assertIsNone(moved["parent_group_id"])
        self.assertEqual(moved["depth"], 0)

    def test_delete_group_only_ungroups_no_cascade(self):
        a = self.svc.create_group("A")
        b = self.svc.create_group("B", a["id"])
        t1 = self._create(group_id=a["id"])
        t2 = self._create(group_id=b["id"])
        before = len(self.svc.list_targets()["items"])
        self.svc.delete_group(a["id"], a["revision"])
        after = self.svc.list_targets()["items"]
        self.assertEqual(len(after), before)  # 无级联删除
        tmap = {t["id"]: t for t in after}
        self.assertIsNone(tmap[t1["id"]]["group_id"])  # 直系目标移至未分组
        self.assertEqual(tmap[t2["id"]]["group_id"], b["id"])
        groups = {g["id"]: g for g in self.svc.list_groups()}
        self.assertNotIn(a["id"], groups)
        self.assertIsNone(groups[b["id"]]["parent_group_id"])  # 子组提升
        self.assertEqual(groups[b["id"]]["depth"], 0)


class ListTargetsTest(_ServiceFixture):
    def test_group_filter_semantics(self):
        a = self.svc.create_group("A")
        t1 = self._create(name="在组内", group_id=a["id"])
        t2 = self._create(name="未分组")
        self.assertEqual(self.svc.list_targets(a["id"])["total"], 1)
        self.assertEqual(self.svc.list_targets(None)["total"], 1)  # 未分组
        self.assertEqual(self.svc.list_targets()["total"], 2)  # 键缺省=不过滤

    def test_query_substring_and_sort_and_page(self):
        g = self.svc.create_group("A")
        for i in range(5):
            self._create(name="按钮%d" % i, group_id=g["id"])
        self._create(name="别的")
        result = self.svc.list_targets(g["id"], query="按钮")
        self.assertEqual(result["total"], 5)
        self.assertEqual([t["sample_no"] for t in result["items"]], [1, 2, 3, 4, 5])
        page = self.svc.list_targets(g["id"], query="按钮", offset=1, limit=2)
        self.assertEqual([t["sample_no"] for t in page["items"]], [2, 3])
        self.assertEqual(page["total"], 5)


class LimitsTest(_ServiceFixture):
    def test_group_count_limit(self):
        groups = tuple(
            {
                "id": "g%d" % i,
                "revision": 1,
                "name": "组%04d" % i,
                "parent_group_id": None,
                "depth": 0,
                "created_at": _TS,
                "updated_at": _TS,
            }
            for i in range(MAX_GROUPS)
        )
        self.repo.commit(CatalogChange(groups=groups), expected_generation=None)
        with self.assertRaises(StorageError) as ctx:
            self.svc.create_group("溢出")
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")

    def test_target_count_limit(self):
        bgr = bytes([1, 2, 3]) * 16
        png = encode_png_bgr(bgr, 4, 4)
        sha = hashlib.sha256(png).hexdigest()
        image = {
            "path": "images/%s.png" % sha,
            "sha256": sha,
            "width": 4,
            "height": 4,
            "format": "png",
            "storage_mode": "png_lossless",
        }
        write = AssetWrite(raw=png, width=4, height=4, format="png", storage_mode="png_lossless")
        targets = tuple(
            {
                "id": "t%05d" % i,
                "sample_no": i,
                "revision": 1,
                "name": "目标%05d" % i,
                "group_id": None,
                "image": image,
                "thumbnail_path": None,
                "anchor_condition_id": "c1",
                "conditions": _cond(),
                "created_at": _TS,
                "updated_at": _TS,
                "metadata": {},
            }
            for i in range(1, MAX_TARGETS + 1)
        )
        self.repo.commit(
            CatalogChange(targets=targets, assets=(write,), next_sample_no=MAX_TARGETS + 1),
            expected_generation=None,
        )
        with self.assertRaises(StorageError) as ctx:
            self._create()
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")


class CatalogChangedEventTest(_ServiceFixture):
    def test_event_emitted_once_after_success_with_new_revision(self):
        target = self._create()
        self.assertEqual(len(self.emitter.events), 1)
        self.assertEqual(self.emitter.events[0]["revision"], target["revision"])
        self.assertTrue(self.emitter.events[0]["generation"].startswith("g-"))

    def test_delete_event_carries_expected_revision(self):
        target = self._create()
        self.emitter.events.clear()
        self.svc.delete_target(target["id"], target["revision"])
        self.assertEqual(len(self.emitter.events), 1)
        self.assertEqual(self.emitter.events[0]["revision"], target["revision"])


class CrossModuleIntegrationTest(unittest.TestCase):
    """§3 必验项：捕获/导入签发的 image_id 经**共享** SessionImageStore 转为 ImageAsset。

    复刻 ``main()`` 装配（同一 ``SessionImageStore`` 实例包裹为资产存储注入 Repository）：
    若按设计稿先前默认（Repository 自建 session_id="repository" 的 store），则所有权
    校验跨 store 必然失败（全部 IMAGE_EXPIRED）——本例同时断言缺陷与修正。
    """

    def test_capture_issued_image_id_converts_and_asset_persisted(self):
        data_dir = tempfile.mkdtemp(prefix="st-svc-integration-")
        session_id = "session-integration"
        # 捕获/导入/宿主帧共用的会话级图像存储（main.py:801）
        shared = SessionImageStore(session_id=session_id)
        # main() 注入的共享资产存储（包裹同一实例）
        asset_store = ImageStore(shared, DataRoot(data_dir))
        repo = Repository(data_dir, image_store=asset_store)
        emitter = RecordingCatalogEmitter()
        svc = TargetService(repo, emitter, clock=lambda: _TS, id_gen=lambda: "t-int")
        # 捕获签发的 ImageRef
        ref = shared.put(RawImage(width=8, height=6, data=bytes([200, 100, 50]) * 48), "png")

        target = svc.create_target(
            {
                "name": "捕获样本",
                "image_id": ref.image_id,
                "anchor_condition_id": "c1",
                "conditions": _cond(),
            }
        )
        # create_target 成功、资产落到 images/<sha256>.png（内容寻址、真实 PNG）
        asset = validate_image_asset(target["image"])
        self.assertTrue(asset.path.startswith("images/"))
        self.assertTrue(asset.path.endswith(".png"))
        asset_path = os.path.join(data_dir, asset.path)
        self.assertTrue(os.path.isfile(asset_path))
        with open(asset_path, "rb") as handle:
            self.assertEqual(handle.read(8), _PNG_SIG)
        self.assertEqual(os.path.basename(asset.path), "%s.png" % asset.sha256)
        # Repository 一致性校验（hash）通过
        repo.image_store.verify(asset)
        # 事件已发（一次）
        self.assertEqual(len(emitter.events), 1)

    def test_default_repository_self_built_store_would_misplace_image(self):
        """反证：未注入共享存储时，Repository 自建 session_id="repository" 的 store，
        capture 签发的 image_id 到该 store 中查必然 IMAGE_EXPIRED（§3 缺陷）。"""
        data_dir = tempfile.mkdtemp(prefix="st-svc-defect-")
        shared = SessionImageStore(session_id="session-x")
        ref = shared.put(RawImage(width=4, height=4, data=bytes([1, 2, 3]) * 16), "png")
        # 未注入 image_store → Repository 自建独立 store
        repo = Repository(data_dir)
        with self.assertRaises(Exception) as ctx:
            repo.image_store.get(ref.image_id)
        self.assertEqual(getattr(ctx.exception, "code", None), "IMAGE_EXPIRED")


if __name__ == "__main__":
    unittest.main()
