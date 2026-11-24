# -*- coding: utf-8 -*-
"""TASK-011 领域层单测（backend/targets/domain.py，纯函数）。

覆盖：名称键（NFC+casefold）、草稿校验（键集/名称/组存在性/storage_mode/条件树）、
编号分配（只增不复用）、补丁白名单、组层级解析、同层同名、整棵子树搬移/删除重算。
全部为纯函数测试，无 IO、无落盘、不发事件。
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.storage.catalog import CatalogSnapshot  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.targets import domain  # noqa: E402

_TS = "2026-09-21T09:00:00Z"


def _cond(anchor: str = "c1") -> dict:
    return {"op": "all", "children": [{"id": anchor, "type": "template", "params": {"threshold": 0.85}}]}


def _image_dict(sha: str = "a" * 64) -> dict:
    return {
        "path": "images/%s.png" % sha,
        "sha256": sha,
        "width": 4,
        "height": 4,
        "format": "png",
        "storage_mode": "original",
    }


def _target(tid: str, sample_no: int, group_id=None, revision: int = 1) -> dict:
    return {
        "id": tid,
        "sample_no": sample_no,
        "revision": revision,
        "name": "目标 %s" % tid,
        "group_id": group_id,
        "image": _image_dict(),
        "thumbnail_path": None,
        "anchor_condition_id": "c1",
        "conditions": _cond(),
        "created_at": _TS,
        "updated_at": _TS,
        "metadata": {},
    }


def _group(gid: str, name: str, parent=None, depth: int = 0, revision: int = 1) -> dict:
    return {
        "id": gid,
        "revision": revision,
        "name": name,
        "parent_group_id": parent,
        "depth": depth,
        "created_at": _TS,
        "updated_at": _TS,
    }


def _snapshot(targets=(), groups=(), next_sample_no=None) -> CatalogSnapshot:
    return CatalogSnapshot(
        generation=None,
        manifest=None,
        targets=tuple(targets),
        groups=tuple(groups),
        script_metadata=None,
        next_sample_no=next_sample_no,
    )


def _code(ctx) -> str:
    return getattr(ctx.exception, "code", None)


def _field(exc) -> str:
    return getattr(exc, "field", None) or exc.details.get("field")


class NameKeyTest(unittest.TestCase):
    def test_nfc_and_casefold(self):
        # e + 组合尖音符 vs 预组合 é → NFC 后相等
        self.assertEqual(domain.name_key("e\u0301"), domain.name_key("\u00e9"))
        # 大小写折叠
        self.assertEqual(domain.name_key("CAFÉ"), domain.name_key("café"))
        # 去首尾空白
        self.assertEqual(domain.name_key("  ABC  "), domain.name_key("abc"))

    def test_non_string_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.name_key(123)  # type: ignore[arg-type]
        self.assertEqual(_code(ctx), "INVALID_PARAMS")


class NormalizeNameTest(unittest.TestCase):
    def test_valid_trims_and_returns(self):
        self.assertEqual(domain.normalize_name("  名称  ", "name"), "名称")

    def test_blank_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.normalize_name("   ", "name")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx.exception), "name")

    def test_101_codepoints_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.normalize_name("名" * 101, "name")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")

    def test_100_codepoints_accepted(self):
        self.assertEqual(len(domain.normalize_name("名" * 100, "name")), 100)


class ValidateDraftTest(unittest.TestCase):
    def _draft(self, **over):
        base = {
            "name": "开始按钮",
            "image_id": "img-1",
            "anchor_condition_id": "c1",
            "conditions": _cond(),
        }
        base.update(over)
        return base

    def test_valid_minimal(self):
        v = domain.validate_draft(_snapshot(), self._draft())
        self.assertEqual(v.name, "开始按钮")
        self.assertEqual(v.storage_mode, "original")  # Q7 缺省
        self.assertIsNone(v.group_id)

    def test_unknown_key_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), self._draft(metadata={}))
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx.exception), "metadata")  # Q4：草稿不含 metadata

    def test_missing_required_key(self):
        draft = self._draft()
        del draft["conditions"]
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), draft)
        self.assertEqual(_field(ctx.exception), "conditions")

    def test_storage_mode_without_image_id(self):
        draft = self._draft()
        del draft["image_id"]
        draft["storage_mode"] = "png_lossless"
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), draft)
        self.assertEqual(_field(ctx.exception), "storage_mode")  # Q7

    def test_storage_mode_invalid_value(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), self._draft(storage_mode="raw"))
        self.assertEqual(_field(ctx.exception), "storage_mode")

    def test_group_not_found(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), self._draft(group_id="g-missing"))
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")
        self.assertEqual(_field(ctx.exception), "group_id")

    def test_group_present_valid(self):
        snap = _snapshot(groups=[_group("g1", "组一")])
        v = domain.validate_draft(snap, self._draft(group_id="g1"))
        self.assertEqual(v.group_id, "g1")

    def test_anchor_not_leaf(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), self._draft(anchor_condition_id="c9"))
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx.exception), "anchor_condition_id")

    def test_empty_conditions_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.validate_draft(_snapshot(), self._draft(conditions={"op": "all", "children": []}))
        self.assertEqual(_code(ctx), "INVALID_PARAMS")


class AllocateSampleNoTest(unittest.TestCase):
    def test_empty_uses_one(self):
        self.assertEqual(domain.allocate_sample_no(_snapshot()), 1)

    def test_takes_max_of_next_and_max_plus_one(self):
        snap = _snapshot(targets=[_target("t1", 1), _target("t2", 5)], next_sample_no=3)
        self.assertEqual(domain.allocate_sample_no(snap), 6)

    def test_next_sample_no_dominates(self):
        snap = _snapshot(targets=[_target("t1", 1)], next_sample_no=9)
        self.assertEqual(domain.allocate_sample_no(snap), 9)


class UpdatePatchTest(unittest.TestCase):
    def setUp(self):
        self.snap = _snapshot(groups=[_group("g1", "组一")])
        self.target = _target("t1", 1)

    def test_empty_patch_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.apply_update_patch(self.snap, self.target, {})
        self.assertEqual(_field(ctx.exception), "patch")

    def test_forbidden_key(self):
        with self.assertRaises(StorageError) as ctx:
            domain.apply_update_patch(self.snap, self.target, {"sample_no": 9})
        self.assertEqual(_field(ctx.exception), "sample_no")

    def test_unknown_key(self):
        with self.assertRaises(StorageError) as ctx:
            domain.apply_update_patch(self.snap, self.target, {"position": 1})
        self.assertEqual(_field(ctx.exception), "patch.position")

    def test_rename_bumps_revision(self):
        new = domain.apply_update_patch(
            self.snap, self.target, {"name": "新名"}, updated_at="2026-10-01T00:00:00Z"
        )
        self.assertEqual(new["name"], "新名")
        self.assertEqual(new["revision"], 2)
        self.assertEqual(new["sample_no"], 1)
        self.assertEqual(new["updated_at"], "2026-10-01T00:00:00Z")

    def test_move_and_ungroup(self):
        moved = domain.apply_update_patch(self.snap, self.target, {"group_id": "g1"})
        self.assertEqual(moved["group_id"], "g1")
        ungroup = domain.apply_update_patch(self.snap, moved, {"group_id": None})
        self.assertIsNone(ungroup["group_id"])

    def test_move_to_missing_group(self):
        with self.assertRaises(StorageError) as ctx:
            domain.apply_update_patch(self.snap, self.target, {"group_id": "g-x"})
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")

    def test_image_without_asset_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            domain.apply_update_patch(self.snap, self.target, {"image_id": "img-2"})
        self.assertEqual(_field(ctx.exception), "image_id")

    def test_image_with_asset_replaces(self):
        asset = _image_dict("b" * 64)
        new = domain.apply_update_patch(
            self.snap, self.target, {"image_id": "img-2"}, image_asset=asset
        )
        self.assertEqual(new["image"]["sha256"], "b" * 64)


class GroupDepthTest(unittest.TestCase):
    def test_top_level_depth_zero(self):
        self.assertEqual(domain.resolve_group_depth(_snapshot(), None), 0)

    def test_child_depth_parent_plus_one(self):
        snap = _snapshot(groups=[_group("g1", "组一", None, 0), _group("g2", "组二", "g1", 1)])
        self.assertEqual(domain.resolve_group_depth(snap, "g2"), 2)

    def test_missing_parent(self):
        with self.assertRaises(StorageError) as ctx:
            domain.resolve_group_depth(_snapshot(), "g-x")
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")
        self.assertEqual(_field(ctx.exception), "parent_group_id")

    def test_depth_limit(self):
        domain.ensure_group_depth_within_limit(2)
        with self.assertRaises(StorageError) as ctx:
            domain.ensure_group_depth_within_limit(3)
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")
        self.assertEqual(ctx.exception.details.get("field"), "parent_group_id")


class SiblingNameUniqueTest(unittest.TestCase):
    def test_duplicate_same_parent(self):
        snap = _snapshot(groups=[_group("g1", "Café")])
        with self.assertRaises(StorageError) as ctx:
            domain.check_sibling_name_unique(snap, None, "CAFÉ")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx.exception), "name")

    def test_same_name_different_parent_ok(self):
        snap = _snapshot(
            groups=[_group("g1", "组一"), _group("g2", "重名", "g1", 1)]
        )
        domain.check_sibling_name_unique(snap, None, "重名")  # 顶层无重名 → 通过

    def test_exclude_self(self):
        snap = _snapshot(groups=[_group("g1", "组一")])
        domain.check_sibling_name_unique(snap, None, "组一", exclude_id="g1")  # 改名自身不冲突


class ReparentSubtreeTest(unittest.TestCase):
    def _tree(self):
        return _snapshot(
            groups=[
                _group("a", "A", None, 0),
                _group("b", "B", "a", 1),
                _group("c", "C", "b", 2),
                _group("d", "D", None, 0),
            ]
        )

    def test_promote_subtree_to_top(self):
        snap = self._tree()
        groups = domain.reparent_subtree(snap, "b", None)
        by_id = {g["id"]: g for g in groups}
        self.assertIsNone(by_id["b"]["parent_group_id"])
        self.assertEqual(by_id["b"]["depth"], 0)
        self.assertEqual(by_id["c"]["depth"], 1)  # 子树整体 depth-1
        self.assertEqual(by_id["a"]["depth"], 0)

    def test_move_subtree_under_other(self):
        snap = self._tree()
        groups = domain.reparent_subtree(snap, "b", "d")
        by_id = {g["id"]: g for g in groups}
        self.assertEqual(by_id["b"]["parent_group_id"], "d")
        self.assertEqual(by_id["b"]["depth"], 1)
        self.assertEqual(by_id["c"]["depth"], 2)

    def test_depth_overflow_on_move(self):
        snap = _snapshot(
            groups=[
                _group("a", "A", None, 0),
                _group("b", "B", "a", 1),
                _group("c", "C", "b", 2),
                _group("d", "D", None, 0),
            ]
        )
        # 把 depth=2 的 c 子树搬到 a（depth 0）→ 子节点 depth 不变；搬 b（含 c）到 d → c depth 2 ok
        groups = domain.reparent_subtree(snap, "b", "d")
        self.assertEqual({g["id"]: g["depth"] for g in groups}["c"], 2)
        # 把 c 搬到 d（depth0）→ c depth1
        groups2 = domain.reparent_subtree(snap, "c", "d")
        self.assertEqual({g["id"]: g["depth"] for g in groups2}["c"], 1)

    def test_cycle_rejected(self):
        snap = self._tree()
        with self.assertRaises(StorageError) as ctx:
            domain.reparent_subtree(snap, "a", "c")  # 搬到自身子孙
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx.exception), "parent_group_id")


class GroupDeleteTest(unittest.TestCase):
    def test_ungroups_targets_and_promotes_children(self):
        snap = _snapshot(
            targets=[_target("t1", 1, group_id="a"), _target("t2", 2, group_id="b")],
            groups=[
                _group("a", "A", None, 0),
                _group("b", "B", "a", 1),
                _group("c", "C", "b", 2),
            ],
        )
        gids_before = {g["id"] for g in snap.groups}
        groups, targets = domain.apply_group_delete(snap, "a")
        by_id = {g["id"]: g for g in groups}
        self.assertNotIn("a", by_id)
        # 直系子组 b 提升到被删组的父位置（顶层）
        self.assertIsNone(by_id["b"]["parent_group_id"])
        self.assertEqual(by_id["b"]["depth"], 0)
        # 整棵子树 depth-1
        self.assertEqual(by_id["c"]["depth"], 1)
        # 直系目标移至未分组；不级联删除目标
        tmap = {t["id"]: t for t in targets}
        self.assertIsNone(tmap["t1"]["group_id"])
        self.assertEqual(tmap["t2"]["group_id"], "b")
        self.assertEqual(len(targets), 2)  # 无级联删除

    def test_missing_group(self):
        with self.assertRaises(StorageError) as ctx:
            domain.apply_group_delete(_snapshot(), "g-x")
        self.assertEqual(_code(ctx), "GROUP_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
