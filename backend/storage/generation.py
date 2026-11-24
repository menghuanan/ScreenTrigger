# -*- coding: utf-8 -*-
"""generation、manifest 与三 JSON 的**纯构造/序列化**（TASK-010，02 布局）。

无 IO 副作用（IO 经 ``catalog``/``fsops``）；集中定义 generation id、manifest
schema（Q4 授权冻结，见 §9.1）与 generation 目录内三文件（``targets.json``、
``groups.json``、``script_metadata.json``）的文档形态。

冻结的 manifest schema（Q4：02「schema、文件 hashes、计数」的**超集且不冲突**）::

    {
      "schema_version": 2,
      "generation": "g-<不透明非空串>",
      "created_at": "2026-09-21T09:00:00Z",
      "files": [
        {"path": "targets.json",         "sha256": "<64hex>", "bytes": 12345},
        {"path": "groups.json",          "sha256": "<64hex>", "bytes": 1234},
        {"path": "script_metadata.json", "sha256": "<64hex>", "bytes": 56}
      ],
      "counts": {"targets": 10000, "groups": 1000, "scripts": 0, "next_sample_no": 10001}
    }

``current.json`` 仍为 02 原文 ``{generation, manifest_sha256}``
（``manifest_sha256`` = manifest 文件**字节** sha256）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import data_corrupt, schema_unsupported

SCHEMA_VERSION: int = 2
GENERATION_PREFIX: str = "g-"
MANIFEST_NAME: str = "manifest.json"
TARGETS_NAME: str = "targets.json"
GROUPS_NAME: str = "groups.json"
SCRIPT_METADATA_NAME: str = "script_metadata.json"
GENERATION_FILES: Tuple[str, ...] = (TARGETS_NAME, GROUPS_NAME, SCRIPT_METADATA_NAME)
MANIFEST_KEYS: Tuple[str, ...] = ("schema_version", "generation", "created_at", "files", "counts")
COUNT_KEYS: Tuple[str, ...] = ("targets", "groups", "scripts", "next_sample_no")


def new_generation_id() -> str:
    """不透明 generation id（Q4：``g-<uuid4hex>``）。"""
    return GENERATION_PREFIX + uuid.uuid4().hex


def sha256_hex(data: bytes) -> str:
    """字节 sha256（十六进制小写）。"""
    return hashlib.sha256(data).hexdigest()


def encode_json(document: Any) -> bytes:
    """确定性 JSON 编码（紧凑、键排序、UTF-8、无 NaN/Infinity；02）。

    键排序保证**同一逻辑内容 → 同一字节 → 同一 hash**，供 hash 校验用。
    """
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def decode_json(raw: bytes, *, what: str = "JSON") -> Any:
    """严格 JSON 解码；非法（含 NaN/Infinity/重复键）→ DATA_CORRUPT。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise data_corrupt("%s 不是合法 UTF-8: %s" % (what, exc)) from exc

    def _no_duplicates(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        seen: Dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError("duplicate key: %s" % key)
            seen[key] = value
        return seen

    def _reject_constant(name: str) -> Any:
        raise ValueError("非法常量: %s" % name)

    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        raise data_corrupt("%s 解析失败: %s" % (what, exc)) from exc


def minimal_script_metadata() -> Dict[str, Any]:
    """Q1：缺失时的最小信封 ``{"schema_version":2,"scripts":[]}``。"""
    return {"schema_version": SCHEMA_VERSION, "scripts": []}


def script_metadata_document(document: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """返回可落盘的 script_metadata 文档：给定则**原样透传**（Q1），否则最小信封。"""
    if document is None:
        return minimal_script_metadata()
    if not isinstance(document, Mapping):
        raise data_corrupt("script_metadata 必须为对象")
    return dict(document)


def targets_document(targets: Sequence[Mapping[str, Any]], next_sample_no: int) -> Dict[str, Any]:
    """02 targets.json 文档形态 ``{schema_version, next_sample_no, targets:[...]}``。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "next_sample_no": int(next_sample_no),
        "targets": [dict(item) for item in targets],
    }


def groups_document(groups: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """groups.json 文档形态 ``{schema_version, groups:[...]}``。"""
    return {"schema_version": SCHEMA_VERSION, "groups": [dict(item) for item in groups]}


def build_manifest(
    generation: str,
    files: Sequence[Tuple[str, bytes]],
    counts: Mapping[str, int],
    created_at: str,
) -> Dict[str, Any]:
    """构造 manifest（Q4 冻结 schema）。``files`` 为 ``(name, bytes)`` 序列。"""
    file_entries: List[Dict[str, Any]] = []
    for name, raw in files:
        file_entries.append(
            {"path": name, "sha256": sha256_hex(raw), "bytes": len(raw)}
        )
    normalized_counts = {key: int(counts.get(key, 0)) for key in COUNT_KEYS}
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": generation,
        "created_at": created_at,
        "files": file_entries,
        "counts": normalized_counts,
    }


def encode_manifest(manifest: Mapping[str, Any]) -> bytes:
    """manifest → 确定性字节（供落盘并取其 sha256 写 current.json）。"""
    return encode_json(manifest)


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise data_corrupt("manifest.counts.%s 必须为非负整数" % field)
    return value


def parse_manifest(raw: bytes) -> Dict[str, Any]:
    """解析并校验 manifest（冻结 schema）；schema_version>2 → SCHEMA_UNSUPPORTED。"""
    document = decode_json(raw, what="manifest.json")
    if not isinstance(document, dict):
        raise data_corrupt("manifest.json 必须为对象")
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise data_corrupt("manifest.schema_version 必须为整数")
    if version > SCHEMA_VERSION:
        raise schema_unsupported(
            "manifest schema_version=%d 高于支持的 %d（不降级覆盖）" % (version, SCHEMA_VERSION)
        )
    if version != SCHEMA_VERSION:
        raise data_corrupt("manifest.schema_version 必须为 %d" % SCHEMA_VERSION)
    extra = set(document) - set(MANIFEST_KEYS)
    missing = set(MANIFEST_KEYS) - set(document)
    if extra or missing:
        raise data_corrupt(
            "manifest 字段非法（缺 %s / 多 %s）"
            % (sorted(missing), sorted(extra))
        )
    if not isinstance(document.get("generation"), str) or not document["generation"]:
        raise data_corrupt("manifest.generation 必须为非空字符串")
    if not isinstance(document.get("created_at"), str) or not document["created_at"]:
        raise data_corrupt("manifest.created_at 必须为字符串")
    files = document.get("files")
    if not isinstance(files, list):
        raise data_corrupt("manifest.files 必须为数组")
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise data_corrupt("manifest.files 元素字段非法")
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise data_corrupt("manifest.files[].path 必须为非空字符串")
        if not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64:
            raise data_corrupt("manifest.files[].sha256 必须为 64 位十六进制")
        if isinstance(entry.get("bytes"), bool) or not isinstance(entry["bytes"], int):
            raise data_corrupt("manifest.files[].bytes 必须为整数")
    counts = document.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(COUNT_KEYS):
        raise data_corrupt("manifest.counts 字段非法（应为 %s）" % list(COUNT_KEYS))
    for key in COUNT_KEYS:
        _require_int(counts[key], key)
    return document


def manifest_sha256(manifest_bytes: bytes) -> str:
    """current.json.manifest_sha256 = manifest 文件字节 sha256。"""
    return sha256_hex(manifest_bytes)
