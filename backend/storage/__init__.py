# -*- coding: utf-8 -*-
"""backend.storage barrel（TASK-010）。

持久化原语（无领域语义）：数据根路径安全、原子文件、数据根级写锁、generation/
manifest、current.json 与备份恢复、会话级 ImageStore（资产落盘）、SQLite 接口骨架。

领域层 ``backend.targets.repository.Repository`` 组合上述原语实现
``read_snapshot`` / ``commit``（02 提交协议 S0–S11）。
"""

from . import fsops, generation
from .catalog import (
    BACKUP_NAME,
    CURRENT_NAME,
    MAX_GROUP_DEPTH,
    MAX_GROUPS,
    MAX_TARGETS,
    Catalog,
    CatalogSnapshot,
    validate_entities,
)
from .errors import (
    StorageError,
    conflict,
    data_corrupt,
    in_use,
    invalid_params,
    resource_limit,
    schema_unsupported,
    storage_error,
)
from .fsops import (
    REPLACE_BACKOFF_BASE_MS,
    REPLACE_BACKOFF_CAP_MS,
    REPLACE_MAX_ATTEMPTS,
    copy_file,
    fsync_dir,
    fsync_file,
    read_bytes,
    replace_with_retry,
    write_atomic_bytes,
    write_temp_bytes,
)
from .generation import (
    GENERATION_FILES,
    GENERATION_PREFIX,
    GROUPS_NAME,
    MANIFEST_NAME,
    SCRIPT_METADATA_NAME,
    SCHEMA_VERSION,
    TARGETS_NAME,
    build_manifest,
    decode_json,
    encode_json,
    encode_manifest,
    minimal_script_metadata,
    new_generation_id,
    parse_manifest,
    sha256_hex,
)
from .imagestore import ASSET_DIR, AssetWrite, ImageStore
from .lock import DataRootLock, lockfile_path, root_lock
from .paths import (
    GENERATIONS_SUBDIR,
    MANAGED_DIRS,
    DataRoot,
    PathEscapeError,
    is_contained,
)
from .sqlite_adapter import (
    DDL_STATEMENTS,
    MIGRATION_STEPS,
    SQLITE_SCHEMA_VERSION,
    SQLiteAdapter,
)

__all__ = [
    "fsops", "generation",
    # errors
    "StorageError", "storage_error", "data_corrupt", "schema_unsupported",
    "conflict", "in_use", "invalid_params", "resource_limit",
    # paths
    "DataRoot", "PathEscapeError", "MANAGED_DIRS", "GENERATIONS_SUBDIR",
    "is_contained",
    # lock
    "DataRootLock", "root_lock", "lockfile_path",
    # fsops
    "REPLACE_MAX_ATTEMPTS", "REPLACE_BACKOFF_BASE_MS", "REPLACE_BACKOFF_CAP_MS",
    "write_atomic_bytes", "write_temp_bytes", "replace_with_retry",
    "fsync_file", "fsync_dir", "read_bytes", "copy_file",
    # generation
    "SCHEMA_VERSION", "GENERATION_PREFIX", "GENERATION_FILES",
    "MANIFEST_NAME", "TARGETS_NAME", "GROUPS_NAME", "SCRIPT_METADATA_NAME",
    "new_generation_id", "build_manifest", "encode_manifest", "parse_manifest",
    "encode_json", "decode_json", "sha256_hex", "minimal_script_metadata",
    # catalog
    "Catalog", "CatalogSnapshot", "validate_entities",
    "CURRENT_NAME", "BACKUP_NAME", "MAX_TARGETS", "MAX_GROUPS", "MAX_GROUP_DEPTH",
    # imagestore
    "ImageStore", "AssetWrite", "ASSET_DIR",
    # sqlite
    "SQLiteAdapter", "SQLITE_SCHEMA_VERSION", "DDL_STATEMENTS", "MIGRATION_STEPS",
]
