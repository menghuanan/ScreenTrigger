# -*- coding: utf-8 -*-
"""backend.targets barrel（TASK-010/TASK-011）。

- TASK-010：``Repository``（JSON catalog 一致提交原语）。
- TASK-011：目标/分组 CRUD 领域层（``domain``）、编排服务（``TargetService``）、
  ``catalog_changed`` 事件端口（``emitter``）与命令接线（``register_target_commands``）。

分层依赖单向：``commands → service → {domain, repository, emitter}``；Repository
不发事件（TASK-010 Q6）；domain 为纯函数（无 IO/落盘/事件）。
"""

from .commands import register_target_commands
from .domain import (
    UNSET,
    build_group_dict,
    build_target_dict,
    ensure_count_limits,
    name_key,
    normalize_name,
    validate_draft,
)
from .emitter import (
    CatalogEmitter,
    NullCatalogEmitter,
    RecordingCatalogEmitter,
    StdioCatalogEmitter,
)
from .repository import CatalogChange, CommitResult, Repository, check_expected_revisions
from .service import TargetService

__all__ = [
    # repository（TASK-010）
    "Repository",
    "CatalogChange",
    "CommitResult",
    "check_expected_revisions",
    # domain（TASK-011）
    "UNSET",
    "name_key",
    "normalize_name",
    "validate_draft",
    "build_target_dict",
    "build_group_dict",
    "ensure_count_limits",
    # service（TASK-011）
    "TargetService",
    # emitter（TASK-011）
    "CatalogEmitter",
    "StdioCatalogEmitter",
    "NullCatalogEmitter",
    "RecordingCatalogEmitter",
    # commands（TASK-011）
    "register_target_commands",
]
