# -*- coding: utf-8 -*-
"""目标/分组领域层（TASK-011）：纯校验 + 在**不可变**快照副本上的变换。

职责边界（设计 §3）：

- **只**做纯函数：不落盘、不 commit、不读文件、不发事件、不改共享快照；
- 依赖仅限于 ``types.conditions``（权威校验器）、``storage.errors``（06 四字段错误）
  与 ``storage.catalog``（数量/深度常量）以及标准库 ``unicodedata``；
- 所有输入快照/实体按 Mapping 处理，**输出为全新 dict/tuple**，绝不原地修改。

覆盖设计 §4/§5/§6 的领域规则：

- 草稿 ``TargetDraft`` 校验（键集严格限定，``metadata`` 不在草稿内，见 Q4）；
- ``sample_no`` 分配（``max(next_sample_no, max_existing+1)``，只增不复用）；
- 完整 Target/Group dict 构造（revision=1、时间戳由调用方注入）；
- ``update_target`` 白名单补丁应用（禁改 id/sample_no/revision/时间/thumbnail_path）；
- 组层级解析（``depth = 父.depth + 1``，父不存在 → ``GROUP_NOT_FOUND``）；
- 同层同名（``name_key = NFC(name.strip()).casefold()``，仅分组）；
- 整棵子树搬移/删除时的 ``depth`` 重算（删组直系目标移至未分组、子组提升）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..storage.catalog import MAX_GROUPS, MAX_GROUP_DEPTH, MAX_TARGETS
from ..storage.errors import (
    StorageError,
    invalid_params,
    resource_limit,
)
from ..types.capabilities import TypeValidationError
from ..types.conditions import validate_target

# ---------------------------------------------------------------------------
# 常量与哨兵
# ---------------------------------------------------------------------------

#: 「键缺省」哨兵：用于区分 ``update_group`` 的“不搬移/不改名”（键缺省）
#: 与“搬到顶层/显式 null”（键存在且值为 None）。
UNSET: Any = object()

#: 草稿键集（03 逐字：``{name,group_id?,image_id,anchor_condition_id,conditions,storage_mode?}``）。
DRAFT_KEYS: Tuple[str, ...] = (
    "name",
    "group_id",
    "image_id",
    "anchor_condition_id",
    "conditions",
    "storage_mode",
)

#: 草稿必填键（``group_id``/``storage_mode`` 可省）。
DRAFT_REQUIRED: Tuple[str, ...] = ("name", "image_id", "anchor_condition_id", "conditions")

#: 名称长度上限（02：去首尾空白后 1–100 Unicode 码点）。
NAME_MIN = 1
NAME_MAX = 100

#: 图片存储模式（02 ImageAsset.storage_mode 枚举）。
STORAGE_MODES: Tuple[str, ...] = ("original", "png_lossless")
STORAGE_MODE_DEFAULT = "original"

#: ``update_target`` 白名单（03 逐字）。
UPDATE_PATCH_ALLOWED: Tuple[str, ...] = (
    "name",
    "group_id",
    "conditions",
    "anchor_condition_id",
    "metadata",
    "image_id",
    "storage_mode",
)

#: ``update_target`` 显式禁止改写的键（03：禁改 ID/sample_no/revision/时间/thumbnail）。
UPDATE_PATCH_FORBIDDEN: Tuple[str, ...] = (
    "id",
    "sample_no",
    "revision",
    "created_at",
    "updated_at",
    "thumbnail_path",
)

#: 目标实体键集（02 Target；仅用于校验内部构造，不用于草稿）。
TARGET_KEYS: Tuple[str, ...] = (
    "id",
    "sample_no",
    "revision",
    "name",
    "group_id",
    "image",
    "thumbnail_path",
    "anchor_condition_id",
    "conditions",
    "created_at",
    "updated_at",
    "metadata",
)

#: 分组实体键集（02 Group）。
GROUP_KEYS: Tuple[str, ...] = (
    "id",
    "revision",
    "name",
    "parent_group_id",
    "depth",
    "created_at",
    "updated_at",
)

#: 占位 ImageAsset（仅在草稿校验中通过 ``validate_target`` 复用条件树/anchor 规则；
#: 真实资产由 service 经 ImageStore 落盘后替换）。
_PLACEHOLDER_IMAGE: Dict[str, Any] = {
    "path": "images/" + ("0" * 64) + ".png",
    "sha256": "0" * 64,
    "width": 1,
    "height": 1,
    "format": "png",
    "storage_mode": STORAGE_MODE_DEFAULT,
}


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatedDraft:
    """通过校验的草稿（规范化后的名称/默认补齐后的 storage_mode）。"""

    name: str
    image_id: str
    anchor_condition_id: str
    conditions: Any
    group_id: Optional[str]
    storage_mode: str


# ---------------------------------------------------------------------------
# 名称
# ---------------------------------------------------------------------------

def name_key(name: str) -> str:
    """同层同名校验键：``NFC(name.strip()).casefold()``（设计 §6/§8）。"""
    if not isinstance(name, str):
        raise invalid_params("name", "name 必须为字符串")
    return unicodedata.normalize("NFC", name.strip()).casefold()


def normalize_name(name: Any, field: str) -> str:
    """名称校验：去首尾空白后 1–100 Unicode 码点，返回规范化（strip）后的值。

    Raises:
        StorageError: INVALID_PARAMS（类型/长度非法，field 逐字定位）。
    """
    if not isinstance(name, str):
        raise invalid_params(field, "%s 必须为字符串" % field)
    stripped = name.strip()
    if not NAME_MIN <= len(stripped) <= NAME_MAX:
        raise invalid_params(
            field, "%s 去首尾空白后须为 1–%d 字符" % (field, NAME_MAX)
        )
    return stripped


# ---------------------------------------------------------------------------
# 内部：复用权威 validate_target
# ---------------------------------------------------------------------------

def _validate_full_target(candidate: Mapping[str, Any]) -> None:
    """用 ``types.conditions.validate_target`` 校验完整目标并归一错误为 StorageError。"""
    try:
        validate_target(dict(candidate))
    except TypeValidationError as exc:
        raise StorageError(
            exc.code, exc.message, field=exc.field, retryable=exc.retryable
        ) from exc


# ---------------------------------------------------------------------------
# 草稿校验
# ---------------------------------------------------------------------------

def validate_draft(snapshot: Any, draft: Any) -> ValidatedDraft:
    """校验 ``TargetDraft`` 并返回规范化值（不落盘；Q4：草稿不含 metadata）。

    Raises:
        StorageError: INVALID_PARAMS（键/名称/类型/条件树/anchor）/ GROUP_NOT_FOUND（组不存在）。
    """
    if not isinstance(draft, Mapping):
        raise invalid_params("draft", "draft 必须为对象")

    for key in draft:
        if key not in DRAFT_KEYS:
            raise invalid_params(str(key), "TargetDraft 未声明字段: %s" % key)

    # Q7：storage_mode 单独出现而无 image_id → INVALID_PARAMS field=storage_mode
    if "storage_mode" in draft and "image_id" not in draft:
        raise invalid_params("storage_mode", "storage_mode 必须与 image_id 同时提供")

    for key in DRAFT_REQUIRED:
        if key not in draft:
            raise invalid_params(key, "TargetDraft 缺少必填字段: %s" % key)

    name = normalize_name(draft.get("name"), "name")

    image_id = draft.get("image_id")
    if not isinstance(image_id, str) or not image_id:
        raise invalid_params("image_id", "image_id 必须为非空字符串")

    group_id = _resolve_optional_group_ref(snapshot, draft.get("group_id"), "group_id")

    storage_mode = draft.get("storage_mode", STORAGE_MODE_DEFAULT)
    if storage_mode not in STORAGE_MODES:
        raise invalid_params(
            "storage_mode", "storage_mode 必须为 %s 之一" % "/".join(STORAGE_MODES)
        )

    anchor_condition_id = draft.get("anchor_condition_id")
    if not isinstance(anchor_condition_id, str) or not anchor_condition_id:
        raise invalid_params(
            "anchor_condition_id", "anchor_condition_id 必须为非空字符串"
        )

    conditions = draft.get("conditions")
    candidate = _draft_candidate(
        name=name,
        conditions=conditions,
        anchor_condition_id=anchor_condition_id,
        group_id=group_id,
    )
    _validate_full_target(candidate)

    return ValidatedDraft(
        name=name,
        image_id=image_id,
        anchor_condition_id=anchor_condition_id,
        conditions=conditions,
        group_id=group_id,
        storage_mode=storage_mode,
    )


def _draft_candidate(
    *,
    name: str,
    conditions: Any,
    anchor_condition_id: str,
    group_id: Optional[str],
) -> Dict[str, Any]:
    """构造完整目标占位 dict（字段齐全），供 ``validate_target`` 复用条件树规则。"""
    return {
        "id": "draft-placeholder",
        "sample_no": 1,
        "revision": 1,
        "name": name,
        "group_id": group_id,
        "image": dict(_PLACEHOLDER_IMAGE),
        "thumbnail_path": None,
        "anchor_condition_id": anchor_condition_id,
        "conditions": conditions,
        "created_at": "2000-01-01T00:00:00Z",
        "updated_at": "2000-01-01T00:00:00Z",
        "metadata": {},
    }


def _resolve_optional_group_ref(
    snapshot: Any, value: Any, field: str
) -> Optional[str]:
    """解析可空组引用：None → None；字符串 → 必须存在（否则 GROUP_NOT_FOUND）。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise invalid_params(field, "%s 必须为字符串或 null" % field)
    if snapshot is not None and snapshot.group_by_id(value) is None:
        raise StorageError("GROUP_NOT_FOUND", "分组不存在: %s" % value, field=field)
    return value


# ---------------------------------------------------------------------------
# 编号分配
# ---------------------------------------------------------------------------

def allocate_sample_no(snapshot: Any) -> int:
    """分配下一个 ``sample_no``：``max(next_sample_no, max_existing+1)``（只增不复用）。"""
    max_existing = max(
        (int(item["sample_no"]) for item in snapshot.targets), default=0
    )
    previous = int(snapshot.next_sample_no or 0)
    return max(previous, max_existing + 1)


# ---------------------------------------------------------------------------
# 实体构造
# ---------------------------------------------------------------------------

def build_target_dict(
    *,
    target_id: str,
    sample_no: int,
    name: str,
    group_id: Optional[str],
    image: Mapping[str, Any],
    anchor_condition_id: str,
    conditions: Any,
    created_at: str,
    updated_at: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """构造 02 形态的完整 Target dict（revision 固定为 1）。"""
    return {
        "id": target_id,
        "sample_no": int(sample_no),
        "revision": 1,
        "name": name,
        "group_id": group_id,
        "image": dict(image),
        "thumbnail_path": None,
        "anchor_condition_id": anchor_condition_id,
        "conditions": conditions,
        "created_at": created_at,
        "updated_at": updated_at,
        "metadata": dict(metadata) if metadata else {},
    }


def build_group_dict(
    *,
    group_id: str,
    name: str,
    parent_group_id: Optional[str],
    depth: int,
    created_at: str,
    updated_at: str,
) -> Dict[str, Any]:
    """构造 02 形态的完整 Group dict（revision 固定为 1）。"""
    return {
        "id": group_id,
        "revision": 1,
        "name": name,
        "parent_group_id": parent_group_id,
        "depth": int(depth),
        "created_at": created_at,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# update_target 补丁
# ---------------------------------------------------------------------------

def apply_update_patch(
    snapshot: Any,
    target: Mapping[str, Any],
    patch: Any,
    *,
    image_asset: Optional[Mapping[str, Any]] = None,
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """按白名单应用 ``update_target`` 补丁，返回 revision+1 的**新**目标 dict。

    Args:
        image_asset: 当 patch 含 ``image_id`` 时，由 service 经 ImageStore 落盘后传入
            的 ImageAsset dict（本函数不触 IO）。

    Raises:
        StorageError: INVALID_PARAMS（空 patch/白名单外/禁改键/字段非法）/
            GROUP_NOT_FOUND（group_id 不存在）。
    """
    if not isinstance(patch, Mapping) or len(patch) == 0:
        raise invalid_params("patch", "patch 必须为非空对象")

    for key in patch:
        if key in UPDATE_PATCH_FORBIDDEN:
            raise invalid_params(str(key), "update_target 禁止修改字段: %s" % key)
        if key not in UPDATE_PATCH_ALLOWED:
            raise invalid_params("patch.%s" % key, "update_target 白名单外字段: %s" % key)

    if "storage_mode" in patch and "image_id" not in patch:
        raise invalid_params("storage_mode", "storage_mode 必须与 image_id 同时提供")

    new: Dict[str, Any] = dict(target)

    if "name" in patch:
        new["name"] = normalize_name(patch.get("name"), "name")

    if "group_id" in patch:
        new["group_id"] = _resolve_optional_group_ref(
            snapshot, patch.get("group_id"), "group_id"
        )

    if "conditions" in patch:
        new["conditions"] = patch.get("conditions")

    if "anchor_condition_id" in patch:
        value = patch.get("anchor_condition_id")
        if not isinstance(value, str) or not value:
            raise invalid_params(
                "anchor_condition_id", "anchor_condition_id 必须为非空字符串"
            )
        new["anchor_condition_id"] = value

    if "metadata" in patch:
        new["metadata"] = patch.get("metadata")

    if "image_id" in patch:
        if not isinstance(image_asset, Mapping):
            raise invalid_params("image_id", "缺少已落盘图像资产（内部错误）")
        new["image"] = dict(image_asset)

    # 权威校验（含条件树/anchor/metadata/image/name）
    _validate_full_target(new)

    new["revision"] = int(target["revision"]) + 1
    if updated_at is not None:
        new["updated_at"] = updated_at
    return new


# ---------------------------------------------------------------------------
# 分组：层级 / 同层同名 / 搬移 / 删除
# ---------------------------------------------------------------------------

def resolve_group_depth(snapshot: Any, parent_group_id: Optional[str]) -> int:
    """解析新分组的 ``depth``：顶层为 0，否则 ``父.depth + 1``。

    Raises:
        StorageError: GROUP_NOT_FOUND（父组不存在）。
    """
    if parent_group_id is None:
        return 0
    if not isinstance(parent_group_id, str) or not parent_group_id:
        raise invalid_params("parent_group_id", "parent_group_id 必须为字符串或 null")
    parent = snapshot.group_by_id(parent_group_id)
    if parent is None:
        raise StorageError(
            "GROUP_NOT_FOUND", "父分组不存在: %s" % parent_group_id,
            field="parent_group_id",
        )
    return int(parent["depth"]) + 1


def ensure_group_depth_within_limit(depth: int) -> None:
    """层级超限（``depth > MAX_GROUP_DEPTH``）→ RESOURCE_LIMIT field=parent_group_id（Q2）。"""
    if int(depth) > MAX_GROUP_DEPTH:
        raise resource_limit(
            "分组层级不得超过 %d 层（depth 0..%d）" % (MAX_GROUP_DEPTH + 1, MAX_GROUP_DEPTH),
            details={"field": "parent_group_id"},
        )


def ensure_count_limits(
    targets: Sequence[Any],
    groups: Sequence[Any],
) -> None:
    """数量上限预检（**提交前**）→ RESOURCE_LIMIT field=targets / groups。

    设计 §6 校验规则表与 §10 失败路径矩阵要求数量超限错误的 ``details.field``
    定位到 ``targets`` / ``groups``；而 ``backend/storage/catalog.py`` 的
    ``validate_entities`` 兜底校验抛错时**未带 field**（storage 属"只复用不改"）。
    故在领域层对"新建后"的实体元组做一次显式预检，使 ``DispatcherError`` 的
    ``details.field`` 符合契约；storage 层兜底校验仍保留（防绕过本层直接 commit）。

    阈值 ``MAX_TARGETS`` / ``MAX_GROUPS`` 一律从 ``storage.catalog`` 导入，
    **不写死数字**（与 storage 兜底同源，避免漂移）。

    Raises:
        StorageError: RESOURCE_LIMIT（目标 > MAX_TARGETS / 分组 > MAX_GROUPS）。
    """
    if len(targets) > MAX_TARGETS:
        raise resource_limit(
            "目标数 %d 超过上限 %d" % (len(targets), MAX_TARGETS),
            details={"field": "targets"},
        )
    if len(groups) > MAX_GROUPS:
        raise resource_limit(
            "分组数 %d 超过上限 %d" % (len(groups), MAX_GROUPS),
            details={"field": "groups"},
        )


def check_sibling_name_unique(
    snapshot: Any,
    parent_group_id: Optional[str],
    name: str,
    *,
    exclude_id: Optional[str] = None,
) -> None:
    """同层同名（NFC+casefold，仅分组）→ INVALID_PARAMS field=name（Q1）。"""
    key = name_key(name)
    for group in snapshot.groups:
        if exclude_id is not None and group["id"] == exclude_id:
            continue
        if group.get("parent_group_id") != parent_group_id:
            continue
        if name_key(group["name"]) == key:
            raise invalid_params("name", "同层已存在同名分组: %s" % name)


def _subtree_ids(snapshot: Any, group_id: str) -> Tuple[str, ...]:
    """返回以 ``group_id`` 为根的整棵子树 id（深度优先，含根）。"""
    children: Dict[Optional[str], List[str]] = {}
    for group in snapshot.groups:
        children.setdefault(group.get("parent_group_id"), []).append(group["id"])
    ordered: List[str] = []
    seen: set = set()
    stack: List[str] = [group_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        for child in children.get(current, []):
            stack.append(child)
    return tuple(ordered)


def reparent_subtree(
    snapshot: Any,
    group_id: str,
    new_parent_id: Optional[str],
    *,
    max_depth: int = MAX_GROUP_DEPTH,
) -> Tuple[Mapping[str, Any], ...]:
    """把 ``group_id`` 子树搬到 ``new_parent_id`` 之下，重算整棵子树 ``depth``。

    Raises:
        StorageError: INVALID_PARAMS（搬到自身/后代形成环，field=parent_group_id）/
            RESOURCE_LIMIT（重算后任一 depth 超限，field=parent_group_id）。
    """
    group = snapshot.group_by_id(group_id)
    if group is None:
        raise StorageError("GROUP_NOT_FOUND", "分组不存在: %s" % group_id, field="group_id")

    subtree = set(_subtree_ids(snapshot, group_id))
    if new_parent_id is not None and new_parent_id in subtree:
        raise invalid_params(
            "parent_group_id", "不得把分组移动到自身或其子孙之下"
        )

    new_root_depth = 0
    if new_parent_id is not None:
        parent = snapshot.group_by_id(new_parent_id)
        if parent is None:
            raise StorageError(
                "GROUP_NOT_FOUND", "父分组不存在: %s" % new_parent_id,
                field="parent_group_id",
            )
        new_root_depth = int(parent["depth"]) + 1
    delta = new_root_depth - int(group["depth"])

    result: List[Mapping[str, Any]] = []
    for item in snapshot.groups:
        if item["id"] == group_id:
            updated = dict(item)
            updated["parent_group_id"] = new_parent_id
            updated["depth"] = int(item["depth"]) + delta
        elif item["id"] in subtree:
            updated = dict(item)
            updated["depth"] = int(item["depth"]) + delta
        else:
            updated = dict(item)
        if int(updated["depth"]) > max_depth:
            raise resource_limit(
                "搬移后分组层级不得超过 %d 层" % (max_depth + 1),
                details={"field": "parent_group_id"},
            )
        result.append(updated)
    return tuple(result)


def apply_group_delete(
    snapshot: Any, group_id: str
) -> Tuple[Tuple[Mapping[str, Any], ...], Tuple[Mapping[str, Any], ...]]:
    """删除分组：直系目标移至未分组、直系子组提升、整棵子树 ``depth-1``。

    不级联删除目标（02）；单事务由 service 一次 ``commit`` 保证。

    Returns:
        ``(new_groups, new_targets)``（均为全新 tuple）。

    Raises:
        StorageError: GROUP_NOT_FOUND（分组不存在）。
    """
    group = snapshot.group_by_id(group_id)
    if group is None:
        raise StorageError("GROUP_NOT_FOUND", "分组不存在: %s" % group_id, field="group_id")

    parent_of_deleted = group.get("parent_group_id")
    subtree = set(_subtree_ids(snapshot, group_id))

    new_groups: List[Mapping[str, Any]] = []
    for item in snapshot.groups:
        if item["id"] == group_id:
            continue
        updated = dict(item)
        # 直系子组提升到被删组的父位置
        if item.get("parent_group_id") == group_id:
            updated["parent_group_id"] = parent_of_deleted
        # 整棵子树（不含被删根）depth-1
        if item["id"] in subtree:
            updated["depth"] = int(item["depth"]) - 1
        new_groups.append(updated)

    new_targets: List[Mapping[str, Any]] = []
    for target in snapshot.targets:
        if target.get("group_id") == group_id:
            updated = dict(target)
            updated["group_id"] = None
            new_targets.append(updated)
        else:
            new_targets.append(dict(target))

    return tuple(new_groups), tuple(new_targets)


def with_group_updated(
    group: Mapping[str, Any],
    *,
    name: Optional[str] = None,
    parent_group_id: Any = UNSET,
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """在（已重算层级的）分组 dict 上应用改名/父指针，revision+1。"""
    updated = dict(group)
    if name is not None:
        updated["name"] = name
    if parent_group_id is not UNSET:
        updated["parent_group_id"] = parent_group_id
    updated["revision"] = int(group["revision"]) + 1
    if updated_at is not None:
        updated["updated_at"] = updated_at
    return updated
