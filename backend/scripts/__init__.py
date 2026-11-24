# -*- coding: utf-8 -*-
"""``backend.scripts`` barrel（TASK-012）：脚本元数据/源码存储/命令。

- ``domain``：纯领域（源码 UTF-8/字节预算、参数校验、Script 构造、元数据文档读写、
  script 定位/revision 判定；无 IO）。
- ``ScriptSourceStore``：内容寻址源码存储（``scripts/<id>/<sha>.lua``，幂等/不可变/回读校验）。
- ``ScriptService``：编排「读快照 → 校验 → 落源码 → 元数据 → 一致提交 → 发事件」。
- ``register_script_commands``：5 条命令（``list_scripts``/``load_script``/``save_script``/
  ``delete_script``/``validate_script``）白名单/编解码/错误映射。

分层依赖单向：``commands → service → {domain, sources, validator, repository, emitter}``；
Repository 不发事件（TASK-010 Q6）；domain 为纯函数（无 IO/落盘/事件）。
"""

from __future__ import annotations

from . import domain
from .commands import register_script_commands
from .domain import (
    SOURCE_MAX_BYTES,
    UNSET,
    build_script_dict,
    is_noop_save,
    script_metadata_document,
    scripts_from_snapshot,
    source_relative_path,
    validate_parameters,
    validate_script_input,
    validate_source,
)
from .service import (
    PAGE_DEFAULT_LIMIT,
    PAGE_DEFAULT_OFFSET,
    PAGE_MAX_LIMIT,
    ScriptService,
)
from .sources import ScriptSourceStore

__all__ = [
    # domain
    "domain",
    "UNSET",
    "SOURCE_MAX_BYTES",
    "scripts_from_snapshot",
    "script_metadata_document",
    "build_script_dict",
    "is_noop_save",
    "source_relative_path",
    "validate_source",
    "validate_parameters",
    "validate_script_input",
    # sources
    "ScriptSourceStore",
    # service
    "ScriptService",
    "PAGE_DEFAULT_OFFSET",
    "PAGE_DEFAULT_LIMIT",
    "PAGE_MAX_LIMIT",
    # commands
    "register_script_commands",
]
