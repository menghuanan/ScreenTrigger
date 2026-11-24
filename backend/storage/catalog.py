# -*- coding: utf-8 -*-
"""current.json 与 generation 目录的读写、快照加载与备份恢复（TASK-010）。

02「磁盘布局与一致提交」：``catalog/current.json = {generation, manifest_sha256}``，
generation 目录含 ``manifest.json`` + 三 JSON（targets/groups/script_metadata）。

- ``CatalogSnapshot``：**不可变**一致快照（frozen dataclass；内部 tuple）。
- ``Catalog``：只认 ``current.json`` 与 ``catalog/generations/<generation>``；
  「校验通过才切 current」；坏 ``current`` → **只读检查备份**（不自动建空库）。
- 实体/FK 校验复用 ``backend.types`` 纯校验器（§8）；失败归一为 ``DATA_CORRUPT``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from backend.types.capabilities import TypeValidationError
from backend.types.conditions import validate_group_fields, validate_target

from . import fsops, generation
from .errors import StorageError, data_corrupt, invalid_params, resource_limit, schema_unsupported
from .generation import (
    GENERATION_FILES,
    GROUPS_NAME,
    MANIFEST_NAME,
    SCRIPT_METADATA_NAME,
    SCHEMA_VERSION,
    TARGETS_NAME,
)
from .paths import DataRoot

CURRENT_NAME: str = "current.json"
BACKUP_NAME: str = "catalog-current.json.bak"

# 06：10000 目标、1000 分组（用户确认的工作限额）
MAX_TARGETS: int = 10000
MAX_GROUPS: int = 1000
MAX_GROUP_DEPTH: int = 2

Logger = Callable[[str], None]


# ---------------------------------------------------------------------------
# 实体/FK 校验（复用 types 纯校验器；仓库与加载共用）
# ---------------------------------------------------------------------------

def validate_entities(
    targets: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
) -> None:
    """纯校验 targets/groups：字段 + 唯一性 + 外键 + 深度 + 数量限额。

    Raises:
        StorageError: DATA_CORRUPT（字段/唯一/外键/深度）/ RESOURCE_LIMIT（超数量）。
    """
    if len(targets) > MAX_TARGETS:
        raise resource_limit("目标数 %d 超过上限 %d" % (len(targets), MAX_TARGETS))
    if len(groups) > MAX_GROUPS:
        raise resource_limit("分组数 %d 超过上限 %d" % (len(groups), MAX_GROUPS))

    group_by_id: Dict[str, Mapping[str, Any]] = {}
    for group in groups:
        try:
            validate_group_fields(group)
        except TypeValidationError as exc:
            raise data_corrupt("分组实体非法: %s" % exc.message, field=exc.field) from exc
        gid = group["id"]
        if gid in group_by_id:
            raise data_corrupt("分组 ID 重复: %s" % gid, field="group_id")
        group_by_id[gid] = group
    for group in groups:
        parent = group.get("parent_group_id")
        if parent is None:
            continue
        if parent not in group_by_id:
            raise data_corrupt("分组 parent_group_id 外键缺失: %s" % parent, field="parent_group_id")
        if group["depth"] != group_by_id[parent]["depth"] + 1:
            raise data_corrupt(
                "分组 depth 与父不一致: %s" % group["id"], field="group.depth"
            )

    target_ids = set()
    sample_nos = set()
    for target in targets:
        try:
            validate_target(target)
        except TypeValidationError as exc:
            raise data_corrupt("目标实体非法: %s" % exc.message, field=exc.field) from exc
        tid = target["id"]
        if tid in target_ids:
            raise data_corrupt("目标 ID 重复: %s" % tid, field="id")
        target_ids.add(tid)
        sample_no = target["sample_no"]
        if sample_no in sample_nos:
            raise data_corrupt("sample_no 不唯一: %d" % sample_no, field="sample_no")
        sample_nos.add(sample_no)
        group_id = target.get("group_id")
        if group_id is not None and group_id not in group_by_id:
            raise data_corrupt("目标 group_id 外键缺失: %s" % group_id, field="group_id")


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CatalogSnapshot:
    """一致快照（不可变）：一整套 targets/groups/script_metadata + generation。"""

    generation: Optional[str]
    manifest: Optional[Mapping[str, Any]]
    targets: Tuple[Mapping[str, Any], ...]
    groups: Tuple[Mapping[str, Any], ...]
    script_metadata: Optional[Mapping[str, Any]]
    next_sample_no: Optional[int]

    @classmethod
    def empty(cls) -> "CatalogSnapshot":
        """空快照：尚无任何 generation（首次提交前）。**不是**空库覆盖。"""
        return cls(
            generation=None,
            manifest=None,
            targets=(),
            groups=(),
            script_metadata=None,
            next_sample_no=None,
        )

    @property
    def is_empty(self) -> bool:
        return self.generation is None

    def target_by_id(self, target_id: str) -> Optional[Mapping[str, Any]]:
        for target in self.targets:
            if target["id"] == target_id:
                return target
        return None

    def group_by_id(self, group_id: str) -> Optional[Mapping[str, Any]]:
        for group in self.groups:
            if group["id"] == group_id:
                return group
        return None

    def revision_of(self, entity_id: str) -> Optional[int]:
        """实体（目标或分组）的当前 revision；不存在返回 None（供 Q5 冲突校验）。"""
        target = self.target_by_id(entity_id)
        if target is not None:
            return int(target["revision"])
        group = self.group_by_id(entity_id)
        if group is not None:
            return int(group["revision"])
        return None


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class Catalog:
    """current.json + generation 目录的 IO 与备份恢复（不持锁，锁由 Repository 持）。"""

    def __init__(
        self,
        data_root: DataRoot,
        *,
        logger: Optional[Logger] = None,
        replace_max_attempts: int = fsops.REPLACE_MAX_ATTEMPTS,
        sleep: Optional[Callable[[float], None]] = None,
        backup_name: str = BACKUP_NAME,
    ) -> None:
        self._root = data_root
        self._logger = logger
        self._replace_max_attempts = replace_max_attempts
        self._sleep = sleep
        self._backup_name = backup_name

    # -- 路径 ----------------------------------------------------------------

    def current_path(self) -> str:
        return os.path.join(self._root.managed_dir("catalog"), CURRENT_NAME)

    def backup_path(self) -> str:
        return os.path.join(self._root.managed_dir("backups"), self._backup_name)

    def generation_dir(self, generation_id: str) -> str:
        if (
            not isinstance(generation_id, str)
            or not generation_id
            or generation_id in (".", "..")
            or "/" in generation_id
            or "\\" in generation_id
        ):
            raise invalid_params("generation", "generation id 非法: %r" % (generation_id,))
        return os.path.join(self._root.generations_dir(), generation_id)

    # -- 读取 ----------------------------------------------------------------

    def read_current(self) -> Optional[Dict[str, Any]]:
        """读取 current.json；不存在返回 None；非法 → DATA_CORRUPT。"""
        path = self.current_path()
        if not os.path.isfile(path):
            return None
        document = generation.decode_json(fsops.read_bytes(path), what="current.json")
        if not isinstance(document, dict) or set(document) != {"generation", "manifest_sha256"}:
            raise data_corrupt("current.json 字段非法（应为 generation/manifest_sha256）")
        if not isinstance(document["generation"], str) or not document["generation"]:
            raise data_corrupt("current.json.generation 必须为非空字符串")
        sha = document["manifest_sha256"]
        if not isinstance(sha, str) or len(sha) != 64:
            raise data_corrupt("current.json.manifest_sha256 必须为 64 位十六进制")
        return document

    def _read_manifest(self, generation_id: str, expected_sha: str) -> Dict[str, Any]:
        manifest_path = os.path.join(self.generation_dir(generation_id), MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            raise data_corrupt("current 指向缺失的 generation: %s" % generation_id)
        raw = fsops.read_bytes(manifest_path)
        if generation.sha256_hex(raw) != expected_sha:
            raise data_corrupt("manifest sha256 与 current.json 不符")
        return generation.parse_manifest(raw)

    def _read_generation_documents(
        self, generation_id: str, manifest: Mapping[str, Any]
    ) -> Dict[str, bytes]:
        gen_dir = self.generation_dir(generation_id)
        documents: Dict[str, bytes] = {}
        for entry in manifest["files"]:
            name = entry["path"]
            if name not in GENERATION_FILES:
                raise data_corrupt("manifest 引用了非 generation 文件: %s" % name)
            path = os.path.join(gen_dir, name)
            if not os.path.isfile(path):
                raise data_corrupt("generation 文件缺失: %s" % name)
            raw = fsops.read_bytes(path)
            if generation.sha256_hex(raw) != entry["sha256"] or len(raw) != entry["bytes"]:
                raise data_corrupt("generation 文件 hash/字节数不符: %s" % name)
            documents[name] = raw
        for name in GENERATION_FILES:
            if name not in documents:
                raise data_corrupt("manifest 缺少 generation 文件: %s" % name)
        return documents

    def load_snapshot(self) -> CatalogSnapshot:
        """从磁盘加载一致快照；坏档 → DATA_CORRUPT（**不建空库**）。

        Raises:
            StorageError: DATA_CORRUPT / SCHEMA_UNSUPPORTED / RESOURCE_LIMIT。
        """
        current = self.read_current()
        if current is None:
            return CatalogSnapshot.empty()
        generation_id = current["generation"]
        manifest = self._read_manifest(generation_id, current["manifest_sha256"])
        documents = self._read_generation_documents(generation_id, manifest)

        targets_doc = generation.decode_json(documents[TARGETS_NAME], what="targets.json")
        targets = _targets_from_document(targets_doc)
        groups_doc = generation.decode_json(documents[GROUPS_NAME], what="groups.json")
        groups = _groups_from_document(groups_doc)
        script_metadata = generation.decode_json(
            documents[SCRIPT_METADATA_NAME], what="script_metadata.json"
        )
        if not isinstance(script_metadata, dict) or script_metadata.get("schema_version") != SCHEMA_VERSION:
            raise data_corrupt("script_metadata.json schema_version 必须为 %d" % SCHEMA_VERSION)

        validate_entities(targets, groups)
        next_sample_no = int(targets_doc["next_sample_no"])
        max_sample = max((int(t["sample_no"]) for t in targets), default=0)
        if next_sample_no <= max_sample:
            raise data_corrupt(
                "next_sample_no(%d) 必须大于已有最大 sample_no(%d)" % (next_sample_no, max_sample)
            )
        return CatalogSnapshot(
            generation=generation_id,
            manifest=manifest,
            targets=tuple(targets),
            groups=tuple(groups),
            script_metadata=script_metadata,
            next_sample_no=next_sample_no,
        )

    # -- 写入 ----------------------------------------------------------------

    def write_generation(self, generation_id: str, documents: Mapping[str, bytes]) -> None:
        """把三 JSON 原子写入 generation 目录（同卷 tmp + fsync + 替换）。"""
        gen_dir = self.generation_dir(generation_id)
        os.makedirs(gen_dir, exist_ok=True)
        for name in GENERATION_FILES:
            path = os.path.join(gen_dir, name)
            fsops.write_atomic_bytes(
                path,
                documents[name],
                max_attempts=self._replace_max_attempts,
                sleep=self._sleep,
                logger=self._logger,
            )

    def write_manifest(self, generation_id: str, manifest_bytes: bytes) -> None:
        """把 manifest.json 原子写入 generation 目录。"""
        gen_dir = self.generation_dir(generation_id)
        os.makedirs(gen_dir, exist_ok=True)
        fsops.write_atomic_bytes(
            os.path.join(gen_dir, MANIFEST_NAME),
            manifest_bytes,
            max_attempts=self._replace_max_attempts,
            sleep=self._sleep,
            logger=self._logger,
        )

    def verify_generation(self, generation_id: str, manifest: Mapping[str, Any]) -> None:
        """回读 generation 目录，校验文件 hash/字节与 manifest 一致。"""
        self._read_generation_documents(generation_id, manifest)

    def activate(self, current_document: Mapping[str, Any]) -> None:
        """同卷临时写 current 指针 → 原子替换（Windows 占用有限重试）。"""
        path = self.current_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        raw = generation.encode_json(dict(current_document))
        tmp = fsops.write_temp_bytes(path, raw)
        fsops.replace_with_retry(
            tmp,
            path,
            max_attempts=self._replace_max_attempts,
            sleep=self._sleep,
            logger=self._logger,
        )

    # -- 备份/恢复 -----------------------------------------------------------

    def backup_current(self) -> bool:
        """把当前 current.json 单份覆盖到 backups/（旧档失败不影响新档）。"""
        path = self.current_path()
        if not os.path.isfile(path):
            return False
        fsops.copy_file(path, self.backup_path())
        return True

    def restore_from_backup(self) -> bool:
        """备份可用且指向完整 generation 时，原子拷贝回 current.json。"""
        backup = self.backup_path()
        if not os.path.isfile(backup):
            return False
        try:
            document = generation.decode_json(fsops.read_bytes(backup), what="backup current")
        except StorageError:
            return False
        if not isinstance(document, dict) or set(document) != {"generation", "manifest_sha256"}:
            return False
        try:
            self._read_manifest(document["generation"], document["manifest_sha256"])
        except StorageError:
            return False
        raw = fsops.read_bytes(backup)
        fsops.write_atomic_bytes(
            self.current_path(),
            raw,
            max_attempts=self._replace_max_attempts,
            sleep=self._sleep,
            logger=self._logger,
        )
        if self._logger is not None:
            self._logger("catalog current.json 已从备份恢复: %s" % document["generation"])
        return True

    def recover(self) -> CatalogSnapshot:
        """只读加载；坏档且备份可用 → 恢复；否则原样 DATA_CORRUPT（不建空库）。"""
        try:
            return self.load_snapshot()
        except StorageError as exc:
            if exc.code != "DATA_CORRUPT" or not self.restore_from_backup():
                raise
            return self.load_snapshot()


def _targets_from_document(document: Any) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(document, dict) or set(document) != {"schema_version", "next_sample_no", "targets"}:
        raise data_corrupt("targets.json 字段非法（应为 schema_version/next_sample_no/targets）")
    _check_document_schema(document.get("schema_version"), "targets.json")
    if isinstance(document.get("next_sample_no"), bool) or not isinstance(document["next_sample_no"], int):
        raise data_corrupt("targets.json.next_sample_no 必须为整数")
    targets = document.get("targets")
    if not isinstance(targets, list):
        raise data_corrupt("targets.json.targets 必须为数组")
    return tuple(targets)


def _groups_from_document(document: Any) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(document, dict) or set(document) != {"schema_version", "groups"}:
        raise data_corrupt("groups.json 字段非法（应为 schema_version/groups）")
    _check_document_schema(document.get("schema_version"), "groups.json")
    groups = document.get("groups")
    if not isinstance(groups, list):
        raise data_corrupt("groups.json.groups 必须为数组")
    return tuple(groups)


def _check_document_schema(version: Any, what: str) -> None:
    if isinstance(version, bool) or not isinstance(version, int):
        raise data_corrupt("%s.schema_version 必须为整数" % what)
    if version > SCHEMA_VERSION:
        raise schema_unsupported(
            "%s schema_version=%d 高于支持的 %d（不降级覆盖）" % (what, version, SCHEMA_VERSION)
        )
    if version != SCHEMA_VERSION:
        raise data_corrupt("%s.schema_version 必须为 %d" % (what, SCHEMA_VERSION))
