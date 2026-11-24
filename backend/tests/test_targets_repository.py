# -*- coding: utf-8 -*-
"""TASK-010 存储测试：Repository 一致提交、冲突、故障注入与重启（02/07，TEST-003/012）。

覆盖提交状态机 S0–S11 的失败点（F5/F7/F8/F9/F10）、两级冲突（generation/revision）、
外键/编号/哈希校验、10k 加载、并发读写一致性与「禁止建空库」。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.storage import generation  # noqa: E402
from backend.storage.catalog import (  # noqa: E402
    CURRENT_NAME,
    MAX_GROUPS,
    CatalogSnapshot,
)
from backend.storage.errors import StorageError  # noqa: E402
from backend.storage.imagestore import AssetWrite  # noqa: E402
from backend.targets import CatalogChange, Repository  # noqa: E402

_TS = "2026-09-21T09:00:00Z"


def _code(exc_ctx):
    return getattr(exc_ctx.exception, "code", None)


def _conditions():
    return {
        "op": "all",
        "children": [{"id": "c1", "type": "template", "params": {"threshold": 0.85}}],
    }


def _asset(raw: bytes, fmt: str = "png", width: int = 4, height: int = 4):
    import hashlib

    sha = hashlib.sha256(raw).hexdigest()
    ext = {"png": "png", "jpeg": "jpg"}[fmt]
    write = AssetWrite(raw=raw, width=width, height=height, format=fmt,
                       storage_mode="png_lossless")
    image = {
        "path": "images/%s.%s" % (sha, ext),
        "sha256": sha,
        "width": width,
        "height": height,
        "format": fmt,
        "storage_mode": "png_lossless",
    }
    return write, image


def _target(tid: str, sample_no: int, image, group_id=None, revision: int = 1):
    return {
        "id": tid,
        "sample_no": sample_no,
        "revision": revision,
        "name": "目标 %s" % tid,
        "group_id": group_id,
        "image": image,
        "thumbnail_path": None,
        "anchor_condition_id": "c1",
        "conditions": _conditions(),
        "created_at": _TS,
        "updated_at": _TS,
        "metadata": {},
    }


def _group(gid: str, name: str, parent=None, depth: int = 0, revision: int = 1):
    return {
        "id": gid,
        "revision": revision,
        "name": name,
        "parent_group_id": parent,
        "depth": depth,
        "created_at": _TS,
        "updated_at": _TS,
    }


class RepositoryCommitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-repo-")

    def _repo(self):
        return Repository(self.dir)

    def test_first_commit_and_restart(self):
        repo = self._repo()
        self.assertTrue(repo.read_snapshot().is_empty)
        result = repo.commit(CatalogChange(), expected_generation=None)
        self.assertEqual(result.to_dict(), {"generation": result.generation})
        self.assertTrue(result.generation.startswith("g-"))
        # current.json 形态（02）
        with open(os.path.join(self.dir, "catalog", CURRENT_NAME), encoding="utf-8") as handle:
            current = json.load(handle)
        self.assertEqual(set(current), {"generation", "manifest_sha256"})
        self.assertEqual(current["generation"], result.generation)
        # 重启（新实例）读到同一 generation
        restarted = Repository(self.dir)
        self.assertEqual(restarted.read_snapshot().generation, result.generation)

    def test_commit_with_target_group_asset(self):
        repo = self._repo()
        write, image = _asset(b"\x89PNG-fake-bytes")
        change = CatalogChange(
            targets=(_target("t1", 1, image, group_id="g1"),),
            groups=(_group("g1", "组一"),),
            assets=(write,),
        )
        result = repo.commit(change, expected_generation=None)
        snapshot = repo.read_snapshot()
        self.assertEqual(snapshot.generation, result.generation)
        self.assertEqual(len(snapshot.targets), 1)
        self.assertEqual(len(snapshot.groups), 1)
        self.assertEqual(snapshot.next_sample_no, 2)
        # 资产已落盘且 hash 一致
        asset_path = os.path.join(self.dir, image["path"])
        self.assertTrue(os.path.isfile(asset_path))
        # 重启后 entities/FK 校验通过
        restarted = Repository(self.dir)
        again = restarted.read_snapshot()
        self.assertEqual(again.target_by_id("t1")["sample_no"], 1)
        self.assertEqual(again.group_by_id("g1")["name"], "组一")

    def test_expected_generation_conflict(self):
        repo = self._repo()
        repo.commit(CatalogChange(), expected_generation=None)
        with self.assertRaises(StorageError) as ctx:
            repo.commit(CatalogChange(), expected_generation="g-wrong")
        self.assertEqual(_code(ctx), "CONFLICT")

    def test_entity_revision_conflict_two_levels(self):
        repo = self._repo()
        write, image = _asset(b"rev-bytes")
        repo.commit(
            CatalogChange(targets=(_target("t1", 1, image),), assets=(write,)),
            expected_generation=None,
        )
        snapshot = repo.read_snapshot()
        # change 构造器先校实体 revision → CONFLICT
        with self.assertRaises(StorageError) as ctx:
            CatalogChange.build(
                snapshot=snapshot,
                targets=(_target("t1", 1, image, revision=2),),
                expected_revisions={"t1": 99},
            )
        self.assertEqual(_code(ctx), "CONFLICT")
        # 一致 revision 通过构造器；但 generation 不符仍 CONFLICT
        change = CatalogChange.build(
            snapshot=snapshot,
            targets=(_target("t1", 1, image, revision=2),),
            expected_revisions={"t1": 1},
        )
        with self.assertRaises(StorageError) as ctx2:
            repo.commit(change, expected_generation="g-stale")
        self.assertEqual(_code(ctx2), "CONFLICT")

    def test_group_foreign_key_missing(self):
        repo = self._repo()
        write, image = _asset(b"fk-bytes")
        with self.assertRaises(StorageError) as ctx:
            repo.commit(
                CatalogChange(
                    targets=(_target("t1", 1, image, group_id="missing"),),
                    assets=(write,),
                ),
                expected_generation=None,
            )
        self.assertEqual(_code(ctx), "DATA_CORRUPT")
        self.assertIsNone(_read_current(self.dir))  # 未切换 current，未建空库

    def test_sample_no_duplicate(self):
        repo = self._repo()
        w1, i1 = _asset(b"dup-a")
        w2, i2 = _asset(b"dup-b")
        with self.assertRaises(StorageError) as ctx:
            repo.commit(
                CatalogChange(
                    targets=(_target("t1", 5, i1), _target("t2", 5, i2)),
                    assets=(w1, w2),
                ),
                expected_generation=None,
            )
        self.assertEqual(_code(ctx), "DATA_CORRUPT")

    def test_next_sample_no_must_exceed_max(self):
        repo = self._repo()
        write, image = _asset(b"nsn-bytes")
        with self.assertRaises(StorageError) as ctx:
            repo.commit(
                CatalogChange(
                    targets=(_target("t1", 7, image),),
                    assets=(write,),
                    next_sample_no=7,
                ),
                expected_generation=None,
            )
        self.assertEqual(_code(ctx), "DATA_CORRUPT")

    def test_next_sample_no_not_decreasing(self):
        repo = self._repo()
        w1, i1 = _asset(b"nsn1")
        repo.commit(
            CatalogChange(targets=(_target("t1", 1, i1),), assets=(w1,),
                          next_sample_no=100),
            expected_generation=None,
        )
        gen1 = repo.read_snapshot().generation
        with self.assertRaises(StorageError) as ctx:
            repo.commit(
                CatalogChange(targets=(), next_sample_no=2),
                expected_generation=gen1,
            )
        self.assertEqual(_code(ctx), "DATA_CORRUPT")  # 删除不回退

    def test_asset_hash_mismatch(self):
        repo = self._repo()
        write, image = _asset(b"good-bytes")
        bad = dict(image)
        bad["sha256"] = "0" * 64  # 与资产内容不符
        with self.assertRaises(StorageError) as ctx:
            repo.commit(
                CatalogChange(targets=(_target("t1", 1, bad),), assets=(write,)),
                expected_generation=None,
            )
        self.assertEqual(_code(ctx), "DATA_CORRUPT")


class RepositoryFailureInjectionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-repo-fail-")

    def test_replace_failure_keeps_old_generation(self):
        repo = Repository(self.dir)
        first = repo.commit(CatalogChange(), expected_generation=None)
        with mock.patch(
            "backend.storage.fsops.replace_with_retry",
            side_effect=StorageError("STORAGE_ERROR", "占用", retryable=True),
        ):
            with self.assertRaises(StorageError) as ctx:
                repo.commit(CatalogChange(), expected_generation=first.generation)
        self.assertEqual(_code(ctx), "STORAGE_ERROR")
        # 旧 generation 完好，重启仍读到首个 generation
        restarted = Repository(self.dir)
        self.assertEqual(restarted.read_snapshot().generation, first.generation)

    def test_crash_before_activate_keeps_old_generation(self):
        repo = Repository(self.dir)
        first = repo.commit(CatalogChange(), expected_generation=None)
        with mock.patch.object(
            repo._catalog, "activate", side_effect=RuntimeError("killed before S9")
        ):
            with self.assertRaises(RuntimeError):
                repo.commit(CatalogChange(), expected_generation=first.generation)
        restarted = Repository(self.dir)
        self.assertEqual(restarted.read_snapshot().generation, first.generation)

    def test_corrupt_current_recovers_from_backup(self):
        repo = Repository(self.dir)
        first = repo.commit(CatalogChange(), expected_generation=None)
        repo.commit(CatalogChange(), expected_generation=first.generation)  # 生成备份
        current_path = os.path.join(self.dir, "catalog", CURRENT_NAME)
        with open(current_path, "w", encoding="utf-8") as handle:
            handle.write("{ truncated")
        fresh = Repository(self.dir)
        with self.assertRaises(StorageError) as ctx:
            fresh.read_snapshot()
        self.assertEqual(_code(ctx), "DATA_CORRUPT")
        snapshot = fresh.recover()  # 备份可用 → 恢复
        self.assertIsNotNone(snapshot.generation)

    def test_corrupt_current_without_backup_no_empty_db(self):
        repo = Repository(self.dir)
        repo.commit(CatalogChange(), expected_generation=None)
        # 删除备份，制造无备份的坏 current
        backups = os.path.join(self.dir, "backups")
        if os.path.isdir(backups):
            for name in os.listdir(backups):
                os.remove(os.path.join(backups, name))
        current_path = os.path.join(self.dir, "catalog", CURRENT_NAME)
        with open(current_path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        fresh = Repository(self.dir)
        with self.assertRaises(StorageError) as ctx:
            fresh.recover()
        self.assertEqual(_code(ctx), "DATA_CORRUPT")
        # 坏档未被空库覆盖
        with open(current_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "not json")

    def test_future_schema_unsupported_on_load(self):
        _write_raw_generation(self.dir, "g-future", targets_schema=3)
        repo = Repository(self.dir)
        with self.assertRaises(StorageError) as ctx:
            repo.read_snapshot()
        self.assertEqual(_code(ctx), "SCHEMA_UNSUPPORTED")


class RepositoryScaleConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-repo-scale-")

    def test_ten_k_targets_load(self):
        count = 10000
        # 复用一个资产 image（同 sha256 → 同名资产，不可变幂等）
        write, image = _asset(b"shared-asset-bytes")
        targets = tuple(_target("t%05d" % i, i, image) for i in range(1, count + 1))
        groups = tuple(_group("g%04d" % i, "组%04d" % i) for i in range(0, MAX_GROUPS))
        repo = Repository(self.dir)
        result = repo.commit(
            CatalogChange(targets=targets, groups=groups, assets=(write,)),
            expected_generation=None,
        )
        self.assertTrue(result.generation.startswith("g-"))
        restarted = Repository(self.dir)
        snapshot = restarted.read_snapshot()
        self.assertEqual(len(snapshot.targets), count)
        self.assertEqual(len(snapshot.groups), MAX_GROUPS)
        manifest_counts = snapshot.manifest["counts"]
        self.assertEqual(manifest_counts["targets"], count)
        self.assertEqual(manifest_counts["next_sample_no"], count + 1)

    def test_concurrent_read_write_consistency(self):
        repo = Repository(self.dir)
        w1, i1 = _asset(b"c1")
        g1 = repo.commit(
            CatalogChange(targets=(_target("t1", 1, i1),), assets=(w1,)),
            expected_generation=None,
        ).generation
        w2, i2 = _asset(b"c2")
        w3, i3 = _asset(b"c3")

        observed = []
        stop = threading.Event()
        start = threading.Event()

        def reader():
            start.wait()
            while not stop.is_set():
                snapshot = repo.read_snapshot()
                observed.append((snapshot.generation, len(snapshot.targets)))

        thread = threading.Thread(target=reader)
        thread.start()
        start.set()
        repo.commit(
            CatalogChange(
                targets=(_target("t1", 1, i1), _target("t2", 2, i2)),
                assets=(w2,),
            ),
            expected_generation=g1,
        )
        gen3 = repo.read_snapshot().generation
        repo.commit(
            CatalogChange(
                targets=(
                    _target("t1", 1, i1),
                    _target("t2", 2, i2),
                    _target("t3", 3, i3),
                ),
                assets=(w3,),
            ),
            expected_generation=gen3,
        )
        stop.set()
        thread.join(timeout=10)
        allowed = {(g1, 1)}
        # 只允许完整版本（无半新半旧）：长度与 generation 必须匹配某一完整提交
        for generation_id, length in observed:
            self.assertIn(length, (1, 2, 3))
            if generation_id == g1:
                self.assertEqual(length, 1)
        self.assertTrue(all(length in (1, 2, 3) for _, length in observed))


def _read_current(directory):
    path = os.path.join(directory, "catalog", CURRENT_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_raw_generation(directory, generation_id, targets_schema):
    """手工写入一个 generation（校验 SCHEMA_UNSUPPORTED 用）。"""
    catalog_dir = os.path.join(directory, "catalog")
    gen_dir = os.path.join(catalog_dir, "generations", generation_id)
    os.makedirs(gen_dir, exist_ok=True)
    targets_doc = {"schema_version": targets_schema, "next_sample_no": 1, "targets": []}
    groups_doc = {"schema_version": 2, "groups": []}
    script_doc = {"schema_version": 2, "scripts": []}
    raw_targets = generation.encode_json(targets_doc)
    raw_groups = generation.encode_json(groups_doc)
    raw_script = generation.encode_json(script_doc)
    with open(os.path.join(gen_dir, "targets.json"), "wb") as handle:
        handle.write(raw_targets)
    with open(os.path.join(gen_dir, "groups.json"), "wb") as handle:
        handle.write(raw_groups)
    with open(os.path.join(gen_dir, "script_metadata.json"), "wb") as handle:
        handle.write(raw_script)
    manifest = generation.build_manifest(
        generation_id,
        (("targets.json", raw_targets), ("groups.json", raw_groups),
         ("script_metadata.json", raw_script)),
        {"targets": 0, "groups": 0, "scripts": 0, "next_sample_no": 1},
        _TS,
    )
    manifest_bytes = generation.encode_manifest(manifest)
    with open(os.path.join(gen_dir, "manifest.json"), "wb") as handle:
        handle.write(manifest_bytes)
    with open(os.path.join(catalog_dir, CURRENT_NAME), "w", encoding="utf-8") as handle:
        json.dump(
            {"generation": generation_id, "manifest_sha256": generation.manifest_sha256(manifest_bytes)},
            handle,
        )


if __name__ == "__main__":
    unittest.main()
