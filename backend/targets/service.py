# -*- coding: utf-8 -*-
"""``TargetService``（TASK-011）：编排「读快照 → 领域变换 → 一致提交 → 发事件」。

职责边界（设计 §3）：

- 读 ``Repository.read_snapshot()`` 得一致快照；调用纯领域函数做变换；拼成**一整套**
  ``CatalogChange`` 后一次 ``Repository.commit``；**仅当 commit 成功后**经
  ``CatalogEmitter`` 发 ``catalog_changed``；
- 承担 ``image_id`` → ``ImageAsset`` 的转换责任（会话内 ``ImageStore.get`` 校验所有权，
  编码为不可变资产后 ``ImageStore.persist``，同一 ``AssetWrite`` 一并进 ``change.assets``
  保证“一起提交”）；
- **不含**存储 IO 细节（归 Repository/storage）；**不发**业务事件以外内容。

决策顺序（02/TASK-010 Q5）：``CatalogChange.build`` **先**校实体 ``expected_revisions``
→ ``commit`` **后**校 ``expected_generation``；任一不符 → ``CONFLICT``。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..capture.errors import CaptureError
from ..image.png import encode_png_bgr
from ..storage.errors import StorageError, in_use, invalid_params
from ..storage.imagestore import AssetWrite
from ..types.region import ImageAsset
from . import domain
from .domain import UNSET
from .emitter import CatalogEmitter, NullCatalogEmitter
from .repository import CatalogChange, Repository

Clock = Callable[[], str]
IdGen = Callable[[], str]

# 分页缺省（03：offset=0、limit=100；limit 1–500 由命令层校验）
PAGE_DEFAULT_OFFSET = 0
PAGE_DEFAULT_LIMIT = 100
PAGE_MAX_LIMIT = 500


def _default_clock() -> str:
    """UTC RFC3339（秒级，``...Z``）时间戳（02）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_id_gen() -> str:
    """持久实体 ID（02：UUID 生成）。"""
    return str(uuid.uuid4())


def _asset_to_dict(asset: ImageAsset) -> Dict[str, Any]:
    """ImageAsset → 02 字段 dict（目标 ``image`` 字段形态）。"""
    return {
        "path": asset.path,
        "sha256": asset.sha256,
        "width": asset.width,
        "height": asset.height,
        "format": asset.format,
        "storage_mode": asset.storage_mode,
    }


def _target_not_found(target_id: str) -> StorageError:
    return StorageError(
        "TARGET_NOT_FOUND", "目标不存在: %s" % target_id, field="target_id"
    )


def _group_not_found(group_id: str, field: str = "group_id") -> StorageError:
    return StorageError("GROUP_NOT_FOUND", "分组不存在: %s" % group_id, field=field)


class TargetService:
    """目标/分组 CRUD 服务（端口：Repository、CatalogEmitter、clock、id_gen）。

    Attributes:
        repository: TASK-010 的 ``Repository``（read_snapshot / commit / image_store）。
    """

    def __init__(
        self,
        repository: Repository,
        emitter: Optional[CatalogEmitter] = None,
        *,
        clock: Optional[Clock] = None,
        id_gen: Optional[IdGen] = None,
    ) -> None:
        if repository is None:
            raise ValueError("repository 不能为空")
        self._repository = repository
        self._emitter: CatalogEmitter = emitter if emitter is not None else NullCatalogEmitter()
        self._clock: Clock = clock if clock is not None else _default_clock
        self._id_gen: IdGen = id_gen if id_gen is not None else _default_id_gen

    # -- 访问器 --------------------------------------------------------------

    @property
    def repository(self) -> Repository:
        return self._repository

    @property
    def emitter(self) -> CatalogEmitter:
        return self._emitter

    @emitter.setter
    def emitter(self, value: CatalogEmitter) -> None:
        """注入/替换事件发射端口（SidecarApp 用 transport/health/session 装配 stdio 实现）。"""
        if value is None:
            raise ValueError("emitter 不能为空")
        self._emitter = value

    def _emit(self, generation: Optional[str], revision: int) -> None:
        """commit 成功后发 ``catalog_changed``（失败路径不得调用本方法）。"""
        self._emitter.emit_catalog_changed(generation=generation, revision=int(revision))

    def _resolve_image(
        self, image_id: str, storage_mode: str
    ) -> Tuple[AssetWrite, Dict[str, Any]]:
        """``image_id`` → ``(AssetWrite, ImageAsset dict)``。

        会话内所有权校验由 ``ImageStore.get`` 完成（过期/跨会话 → ``IMAGE_EXPIRED``，
        归一为 ``StorageError``）；内部图像为 BGR，落盘前编码为不可变 PNG 资产
        （保证 ``images/<sha256>.png`` 为真实图像文件，非原始 BGR 字节）。

        Raises:
            StorageError: IMAGE_EXPIRED / INVALID_PARAMS / STORAGE_ERROR / DATA_CORRUPT。
        """
        store = self._repository.image_store
        try:
            stored = store.get(image_id)
        except CaptureError as exc:
            raise StorageError(
                exc.code, exc.message, details=dict(exc.details), retryable=exc.retryable
            ) from exc
        if storage_mode == "original" and stored.original_bytes is not None:
            raw, image_format = stored.original_bytes, stored.format
        else:
            raw = encode_png_bgr(bytes(stored.data), int(stored.width), int(stored.height))
            image_format = "png"
        write = AssetWrite(
            raw=raw,
            width=int(stored.width),
            height=int(stored.height),
            format=image_format,
            storage_mode=storage_mode,
        )
        asset = store.persist(write)
        return write, _asset_to_dict(asset)

    # -- 目标 ----------------------------------------------------------------

    def create_target(self, draft: Any) -> Dict[str, Any]:
        """新建目标：解析草稿 → 落盘资产 → 分配编号 → 一致提交 → 发事件。"""
        snapshot = self._repository.read_snapshot()
        validated = domain.validate_draft(snapshot, draft)
        write, image_dict = self._resolve_image(validated.image_id, validated.storage_mode)
        sample_no = domain.allocate_sample_no(snapshot)
        now = self._clock()
        target = domain.build_target_dict(
            target_id=self._id_gen(),
            sample_no=sample_no,
            name=validated.name,
            group_id=validated.group_id,
            image=image_dict,
            anchor_condition_id=validated.anchor_condition_id,
            conditions=validated.conditions,
            created_at=now,
            updated_at=now,
        )
        # 数量预检（提交前）：使 RESOURCE_LIMIT 的 details.field 符合设计（field=targets）。
        new_targets = snapshot.targets + (target,)
        domain.ensure_count_limits(new_targets, snapshot.groups)
        change = CatalogChange.build(
            snapshot,
            targets=new_targets,
            groups=snapshot.groups,
            assets=(write,),
            next_sample_no=sample_no + 1,
            expected_revisions={},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(target["revision"]))
        return target

    def get_target(self, target_id: str) -> Dict[str, Any]:
        """按 ID 取目标（只读）。

        Raises:
            StorageError: TARGET_NOT_FOUND。
        """
        snapshot = self._repository.read_snapshot()
        target = snapshot.target_by_id(target_id)
        if target is None:
            raise _target_not_found(target_id)
        return dict(target)

    def list_targets(
        self,
        group_id: Any = UNSET,
        query: Optional[str] = None,
        offset: int = PAGE_DEFAULT_OFFSET,
        limit: int = PAGE_DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        """列表：``group_id``（键缺省=不过滤 / ``null``=未分组 / 字符串=精确该组不递归）
        + 名称子串 ``query``；按 ``sample_no`` 升序；分页（Q5）。"""
        snapshot = self._repository.read_snapshot()
        items: List[Mapping[str, Any]] = list(snapshot.targets)
        if group_id is not UNSET:
            if group_id is None:
                items = [t for t in items if t.get("group_id") is None]
            else:
                items = [t for t in items if t.get("group_id") == group_id]
        if query is not None:
            text = query if isinstance(query, str) else str(query)
            items = [t for t in items if text in str(t.get("name", ""))]
        items.sort(key=lambda t: int(t["sample_no"]))
        total = len(items)
        page = items[offset : offset + limit]
        return {"items": [dict(t) for t in page], "total": total}

    def update_target(
        self, target_id: str, patch: Any, expected_revision: int
    ) -> Dict[str, Any]:
        """按白名单补丁更新目标（禁改 ID/编号/revision/时间；图片替换可选）。"""
        snapshot = self._repository.read_snapshot()
        target = snapshot.target_by_id(target_id)
        if target is None:
            raise _target_not_found(target_id)

        assets: Tuple[AssetWrite, ...] = ()
        image_asset: Optional[Dict[str, Any]] = None
        if isinstance(patch, Mapping) and "image_id" in patch:
            storage_mode = patch.get("storage_mode", domain.STORAGE_MODE_DEFAULT)
            if storage_mode not in domain.STORAGE_MODES:
                raise invalid_params(
                    "storage_mode",
                    "storage_mode 必须为 %s 之一" % "/".join(domain.STORAGE_MODES),
                )
            write, image_asset = self._resolve_image(str(patch["image_id"]), storage_mode)
            assets = (write,)
        elif isinstance(patch, Mapping) and "storage_mode" in patch:
            raise invalid_params("storage_mode", "storage_mode 必须与 image_id 同时提供")

        now = self._clock()
        new_target = domain.apply_update_patch(
            snapshot, target, patch, image_asset=image_asset, updated_at=now
        )
        new_targets = tuple(
            new_target if item["id"] == target_id else dict(item)
            for item in snapshot.targets
        )
        change = CatalogChange.build(
            snapshot,
            targets=new_targets,
            groups=snapshot.groups,
            assets=assets,
            expected_revisions={target_id: expected_revision},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(new_target["revision"]))
        return new_target

    def delete_target(self, target_id: str, expected_revision: int) -> None:
        """删除目标（活动引用 → IN_USE；编号不回退，由 Repository 保证）。"""
        snapshot = self._repository.read_snapshot()
        target = snapshot.target_by_id(target_id)
        if target is None:
            raise _target_not_found(target_id)
        if self._repository.is_referenced(target_id):
            raise in_use(
                "目标被活动执行引用，无法删除: %s" % target_id,
                details={"field": "target_id"},
            )
        new_targets = tuple(
            dict(item) for item in snapshot.targets if item["id"] != target_id
        )
        change = CatalogChange.build(
            snapshot,
            targets=new_targets,
            groups=snapshot.groups,
            expected_revisions={target_id: expected_revision},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(expected_revision))

    def move_target_to_group(
        self, target_id: str, group_id: Optional[str], expected_revision: int
    ) -> Dict[str, Any]:
        """把目标移动到指定分组（``group_id=null`` → 未分组）。"""
        snapshot = self._repository.read_snapshot()
        target = snapshot.target_by_id(target_id)
        if target is None:
            raise _target_not_found(target_id)
        if group_id is not None:
            if not isinstance(group_id, str) or not group_id:
                raise invalid_params("group_id", "group_id 必须为字符串或 null")
            if snapshot.group_by_id(group_id) is None:
                raise _group_not_found(group_id, field="group_id")
        new_target = dict(target)
        new_target["group_id"] = group_id
        new_target["revision"] = int(target["revision"]) + 1
        new_target["updated_at"] = self._clock()
        new_targets = tuple(
            new_target if item["id"] == target_id else dict(item)
            for item in snapshot.targets
        )
        change = CatalogChange.build(
            snapshot,
            targets=new_targets,
            groups=snapshot.groups,
            expected_revisions={target_id: expected_revision},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(new_target["revision"]))
        return new_target

    # -- 分组 ----------------------------------------------------------------

    def create_group(self, name: Any, parent_group_id: Optional[str] = None) -> Dict[str, Any]:
        """新建分组（缺省/``null`` 父 → 顶层；层级≤3、同层同名唯一）。"""
        snapshot = self._repository.read_snapshot()
        normalized = domain.normalize_name(name, "name")
        depth = domain.resolve_group_depth(snapshot, parent_group_id)
        domain.ensure_group_depth_within_limit(depth)
        domain.check_sibling_name_unique(snapshot, parent_group_id, normalized)
        now = self._clock()
        group = domain.build_group_dict(
            group_id=self._id_gen(),
            name=normalized,
            parent_group_id=parent_group_id,
            depth=depth,
            created_at=now,
            updated_at=now,
        )
        # 数量预检（提交前）：使 RESOURCE_LIMIT 的 details.field 符合设计（field=groups）。
        new_groups = snapshot.groups + (group,)
        domain.ensure_count_limits(snapshot.targets, new_groups)
        change = CatalogChange.build(
            snapshot,
            targets=snapshot.targets,
            groups=new_groups,
            expected_revisions={},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(group["revision"]))
        return group

    def list_groups(self) -> List[Dict[str, Any]]:
        """全量分组（派生 ``target_count``，不持久化；02）。"""
        snapshot = self._repository.read_snapshot()
        counts: Dict[str, int] = {}
        for target in snapshot.targets:
            gid = target.get("group_id")
            if gid is not None:
                counts[gid] = counts.get(gid, 0) + 1
        result: List[Dict[str, Any]] = []
        for group in snapshot.groups:
            item = dict(group)
            item["target_count"] = counts.get(group["id"], 0)
            result.append(item)
        return result

    def update_group(
        self,
        group_id: str,
        expected_revision: int,
        *,
        name: Any = UNSET,
        parent_group_id: Any = UNSET,
    ) -> Dict[str, Any]:
        """改名 / 搬移（子树层级重算）；``name`` 与 ``parent_group_id`` 至少给一个（Q3）。"""
        snapshot = self._repository.read_snapshot()
        group = snapshot.group_by_id(group_id)
        if group is None:
            raise _group_not_found(group_id, field="group_id")
        if name is UNSET and parent_group_id is UNSET:
            raise invalid_params("name", "update_group 需至少提供 name 或 parent_group_id")

        if parent_group_id is not UNSET:
            if parent_group_id is not None:
                if not isinstance(parent_group_id, str) or not parent_group_id:
                    raise invalid_params(
                        "parent_group_id", "parent_group_id 必须为字符串或 null"
                    )
                if snapshot.group_by_id(parent_group_id) is None:
                    raise _group_not_found(parent_group_id, field="parent_group_id")
            groups_after = domain.reparent_subtree(snapshot, group_id, parent_group_id)
            effective_parent: Optional[str] = parent_group_id
        else:
            groups_after = tuple(dict(item) for item in snapshot.groups)
            effective_parent = group.get("parent_group_id")

        base_group = _find_group(groups_after, group_id)
        new_name = base_group["name"] if name is UNSET else domain.normalize_name(name, "name")
        domain.check_sibling_name_unique(
            snapshot, effective_parent, new_name, exclude_id=group_id
        )

        new_group = domain.with_group_updated(
            base_group,
            name=(new_name if name is not UNSET else None),
            updated_at=self._clock(),
        )
        new_groups = tuple(
            new_group if item["id"] == group_id else dict(item) for item in groups_after
        )
        change = CatalogChange.build(
            snapshot,
            targets=snapshot.targets,
            groups=new_groups,
            expected_revisions={group_id: expected_revision},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(new_group["revision"]))
        return new_group

    def delete_group(self, group_id: str, expected_revision: int) -> None:
        """删除分组：直系目标移至未分组、子组提升、整棵子树 depth-1（单事务）。"""
        snapshot = self._repository.read_snapshot()
        group = snapshot.group_by_id(group_id)
        if group is None:
            raise _group_not_found(group_id, field="group_id")
        new_groups, new_targets = domain.apply_group_delete(snapshot, group_id)
        change = CatalogChange.build(
            snapshot,
            targets=new_targets,
            groups=new_groups,
            expected_revisions={group_id: expected_revision},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(expected_revision))


def _find_group(groups: Tuple[Mapping[str, Any], ...], group_id: str) -> Mapping[str, Any]:
    """在给定分组元组中定位分组（内部辅助；调用前已确认存在）。"""
    for group in groups:
        if group["id"] == group_id:
            return group
    raise _group_not_found(group_id, field="group_id")
