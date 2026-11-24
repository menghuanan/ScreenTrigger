# -*- coding: utf-8 -*-
"""TASK-010 契约测试：SQLite 适配器接口骨架与迁移设计（Q8）。

01 M03/02 明文：``SQLiteAdapter`` 必须通过同一 Repository 合约测试，首版只提供
接口与迁移设计，不实现运行时双写/自动迁移。本用例只验证**接口骨架**与建议表结构，
不建立真实 SQLite 连接（无 sqlite3 依赖）。
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.storage.catalog import CatalogSnapshot  # noqa: E402
from backend.storage.sqlite_adapter import (  # noqa: E402
    DDL_STATEMENTS,
    MIGRATION_STEPS,
    SQLiteAdapter,
    migration_metadata,
)
from backend.targets import Repository  # noqa: E402


class SQLiteAdapterSkeletonTest(unittest.TestCase):
    def test_abstract_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            SQLiteAdapter()  # type: ignore[abstract]

    def test_contract_methods_match_repository(self):
        # 同一合约测试的意图：接口方法与 JSON Repository 同名同语义
        for name in ("read_snapshot", "commit", "close"):
            self.assertTrue(hasattr(SQLiteAdapter, name))
            self.assertTrue(callable(getattr(Repository, name)))

    def test_ddl_covers_expected_tables_and_foreign_keys(self):
        joined = "\n".join(DDL_STATEMENTS).lower()
        for table in ("targets", "groups", "scripts", "meta"):
            self.assertIn("create table if not exists %s" % table, joined)
        self.assertIn("foreign_keys = on", joined)
        self.assertIn("on delete set null", joined)
        self.assertIn("depth integer not null check", joined)

    def test_migration_design_present_and_no_runtime_dual_write(self):
        self.assertGreaterEqual(len(MIGRATION_STEPS), 5)
        self.assertFalse(SQLiteAdapter.supports_runtime_dual_write())

    def test_concrete_subclass_instantiable(self):
        class _Dummy(SQLiteAdapter):
            def read_snapshot(self):
                return CatalogSnapshot.empty()

            def commit(self, change, expected_generation=None):
                return None

            def close(self):
                return None

        adapter = _Dummy()
        self.assertTrue(adapter.read_snapshot().is_empty)

    def test_migration_metadata_shape(self):
        metadata = migration_metadata(2, {"targets": 3, "groups": 1}, "a" * 64)
        self.assertEqual(metadata["schema_version"], 2)
        self.assertEqual(metadata["counts"]["targets"], 3)
        self.assertEqual(metadata["content_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
