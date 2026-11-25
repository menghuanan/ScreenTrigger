# -*- coding: utf-8 -*-
"""SQLite 适配器接口骨架与迁移设计（TASK-010，Q8；02「SQLite 后续映射和迁移」）。

01 M03/02 明文：``SQLiteAdapter`` **必须通过同一 Repository 合约测试**，首版
**只提供接口与迁移设计**，**不实现**运行时双写/自动迁移（02「首版不实现自动迁移
或运行时双写」）。本模块只定义 ABC 接口、建议表结构与迁移步骤常量，**无运行时
SQLite 依赖**（不 import sqlite3，避免未实现路径被误用）。
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from .catalog import CatalogSnapshot

if TYPE_CHECKING:
    from backend.targets.repository import CatalogChange, CommitResult


@runtime_checkable
class RepositoryPort(Protocol):
    """The JSON implementation and a future adapter expose the same snapshot/CAS contract."""

    def read_snapshot(self) -> CatalogSnapshot: ...
    def commit(self, change: CatalogChange, expected_generation: Optional[str]) -> CommitResult: ...
    def close(self) -> None: ...

SQLITE_SCHEMA_VERSION: int = 2

# 02「SQLite 后续映射」建议表；启用 foreign_keys。
DDL_STATEMENTS: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS targets ("
    " id TEXT PRIMARY KEY,"
    " sample_no INTEGER NOT NULL UNIQUE,"
    " revision INTEGER NOT NULL,"
    " name TEXT NOT NULL,"
    " group_id TEXT REFERENCES groups(id) ON DELETE SET NULL,"
    " image_json TEXT NOT NULL,"
    " conditions_json TEXT NOT NULL,"
    " anchor_condition_id TEXT NOT NULL,"
    " metadata_json TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS groups ("
    " id TEXT PRIMARY KEY,"
    " revision INTEGER NOT NULL,"
    " name TEXT NOT NULL,"
    " parent_group_id TEXT REFERENCES groups(id),"
    " depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 2),"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS scripts ("
    " id TEXT PRIMARY KEY,"
    " revision INTEGER NOT NULL,"
    " name TEXT NOT NULL,"
    " source_path TEXT NOT NULL,"
    " source_sha256 TEXT NOT NULL,"
    " parameters_json TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meta ("
    " key TEXT PRIMARY KEY,"
    " value TEXT NOT NULL)",
    "PRAGMA foreign_keys = ON",
)

# 迁移步骤（02：无活动执行且持有写锁 → 备份 → 临时库事务导入 → 校验 → 原子切换）。
MIGRATION_STEPS: Tuple[str, ...] = (
    "1. 前置：无活动执行且持有写锁",
    "2. 备份整个有效 generation 与资产",
    "3. 临时数据库事务导入（targets/groups/scripts/meta）",
    "4. 验证 ID/计数/最大编号/外键/hash 与逐实体归一化内容一致",
    "5. 关闭连接 → 原子切换后端选择指针",
    "6. 失败：丢弃临时库、保持 JSON；尚无写入可切回备份",
    "7. 已有新写入：必须逆向导出后再切回，不丢弃新增数据",
)


class SQLiteAdapter(RepositoryPort, abc.ABC):
    """Repository 的 SQLite 实现契约（首版仅接口；须通过同一合约测试）。"""

    @abc.abstractmethod
    def read_snapshot(self) -> CatalogSnapshot:
        """返回一致快照（与 JSON Repository 同语义）。"""
        raise NotImplementedError

    @abc.abstractmethod
    def commit(
        self,
        change: Any,
        expected_generation: Optional[str],
    ) -> Any:
        """提交一个 generation（与 JSON Repository 同语义：CONFLICT/校验/CAS）。"""
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        """释放连接/资源。"""
        raise NotImplementedError

    @classmethod
    def ddl(cls) -> Sequence[str]:
        """建议表结构 DDL（供迁移；首版不落库）。"""
        return DDL_STATEMENTS

    @classmethod
    def migration_steps(cls) -> Sequence[str]:
        """迁移步骤说明（02）。"""
        return MIGRATION_STEPS

    @classmethod
    def supports_runtime_dual_write(cls) -> bool:
        """首版**不**支持运行时双写/自动迁移（02 明文）。"""
        return False


def migration_metadata(
    schema_version: int,
    counts: Mapping[str, int],
    content_sha256: str,
) -> Mapping[str, Any]:
    """迁移记录的元数据（02：schema、计数、内容 hash；验证过后切换）。"""
    return {
        "schema_version": int(schema_version),
        "counts": {key: int(value) for key, value in counts.items()},
        "content_sha256": content_sha256,
    }
