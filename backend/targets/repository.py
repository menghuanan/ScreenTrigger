# -*- coding: utf-8 -*-
"""Repository：JSON catalog 的一致提交与只读快照（TASK-010）。

按 02「磁盘布局与一致提交」提交协议（S0–S11）：

```
S0 获取数据根级单写锁（storage.lock；跨进程第二写者被拒）
S1 读 current.json → 比对 expected_generation / expected_revision（不符 → CONFLICT）
S2 序列化新快照（三 JSON）+ 新 generation id
S3 写新资产（内容寻址、不可变）
S4/S5 写 generation 目录三 JSON 与 manifest
S6 flush/fsync（fsops 内）
S7 校验外键/哈希/计数/编号
S8/S9 同卷临时写 current 指针 → 原子替换（Windows 占用有限重试）
S10 更新内存快照指针
S11 返回 CommitResult{generation}
```

崩溃语义：S9 之前崩溃 → 旧 generation 完整；S9 之后 → 新 generation 完整；
两态均可加载，**绝不半新半旧**；**禁止建空库**。

- ``read_snapshot()`` **不加写锁**（读内存一致快照指针；§6）。
- Repository **不发事件**（00 禁 Repository→UI；Q6）；``commit`` 返回 generation，
  由 TASK-011 handler 在提交成功后发 ``catalog_changed``。
- 不内嵌领域规则（同名/编号/搬移归 TASK-011）；本层只做存储校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from backend.image.store import SessionImageStore
from backend.types.capabilities import TypeValidationError
from backend.types.region import ImageAsset, validate_image_asset

from ..storage import fsops, generation
from ..storage.catalog import (
    Catalog,
    CatalogSnapshot,
    validate_entities,
)
from ..storage.errors import StorageError, conflict, data_corrupt, invalid_params
from ..storage.generation import (
    GROUPS_NAME,
    SCRIPT_METADATA_NAME,
    TARGETS_NAME,
)
from ..storage.imagestore import AssetWrite, ImageStore
from ..storage.lock import DataRootLock
from ..storage.paths import DataRoot

Logger = Callable[[str], None]


def _default_clock() -> str:
    """UTC RFC3339（秒级，``...Z``）时间戳（02）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CommitResult:
    """02：``CommitResult = {generation}``；current 已切换且校验成功。"""

    generation: str
    manifest_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return {"generation": self.generation}


def check_expected_revisions(
    snapshot: CatalogSnapshot,
    expected_revisions: Mapping[str, int],
) -> None:
    """实体级 revision 冲突（Q5）：不符 → CONFLICT。"""
    for entity_id, expected in expected_revisions.items():
        actual = snapshot.revision_of(entity_id)
        if actual is None:
            raise conflict("实体不存在: %s" % entity_id, field="expected_revision")
        if int(actual) != int(expected):
            raise conflict(
                "实体 revision 不符: %s 期望 %s 实际 %s" % (entity_id, expected, actual),
                field="expected_revision",
            )


@dataclass(frozen=True)
class CatalogChange:
    """一次提交的**不可变**输入值（由 TASK-011 构造，Repository 校验+落盘）。

    ``targets``/``groups`` 为 02 形态的实体 dict（字段校验见 ``validate_entities``）；
    ``script_metadata`` 原样透传（Q1）；``assets`` 为待落盘资产；``next_sample_no``
    为 None 时由 Repository 依据旧值/最大值推导（不回退）。
    """

    targets: Tuple[Mapping[str, Any], ...] = ()
    groups: Tuple[Mapping[str, Any], ...] = ()
    script_metadata: Optional[Mapping[str, Any]] = None
    assets: Tuple[AssetWrite, ...] = ()
    next_sample_no: Optional[int] = None
    expected_revisions: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        snapshot: Optional[CatalogSnapshot] = None,
        *,
        targets: Tuple[Mapping[str, Any], ...] = (),
        groups: Tuple[Mapping[str, Any], ...] = (),
        script_metadata: Optional[Mapping[str, Any]] = None,
        assets: Tuple[AssetWrite, ...] = (),
        next_sample_no: Optional[int] = None,
        expected_revisions: Optional[Mapping[str, int]] = None,
    ) -> "CatalogChange":
        """构造 change；给定 snapshot 时**先**校验实体 revision（Q5 顺序）。"""
        revisions = dict(expected_revisions or {})
        if snapshot is not None and revisions:
            check_expected_revisions(snapshot, revisions)
        return cls(
            targets=tuple(targets),
            groups=tuple(groups),
            script_metadata=script_metadata,
            assets=tuple(assets),
            next_sample_no=next_sample_no,
            expected_revisions=revisions,
        )


class Repository:
    """JSON 仓库：数据根级写锁 + 不可变 generation + current 原子指针。"""

    def __init__(
        self,
        data_dir: str,
        *,
        logger: Optional[Logger] = None,
        clock: Optional[Callable[[], str]] = None,
        image_store: Optional[ImageStore] = None,
        reference_port: Optional[Any] = None,
        replace_max_attempts: int = fsops.REPLACE_MAX_ATTEMPTS,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        if not isinstance(data_dir, str) or not data_dir:
            raise ValueError("data_dir 必须为非空字符串")
        self._root = DataRoot(data_dir)
        self._lock = DataRootLock(data_dir)
        self._catalog = Catalog(
            self._root,
            logger=logger,
            replace_max_attempts=replace_max_attempts,
            sleep=sleep,
        )
        if image_store is None:
            image_store = ImageStore(
                SessionImageStore(session_id="repository"), self._root, logger=logger
            )
        self._images = image_store
        self._reference_port = reference_port
        self._clock: Callable[[], str] = clock if clock is not None else _default_clock
        self._logger = logger
        self._snapshot: Optional[CatalogSnapshot] = None

    # -- 访问器 --------------------------------------------------------------

    @property
    def data_root(self) -> DataRoot:
        return self._root

    @property
    def image_store(self) -> ImageStore:
        return self._images

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    # -- 读 ------------------------------------------------------------------

    def read_snapshot(self) -> CatalogSnapshot:
        """返回一致快照（不取写锁；首次从磁盘加载并缓存）。"""
        if self._snapshot is None:
            self._snapshot = self._catalog.load_snapshot()
        return self._snapshot

    def reload(self) -> CatalogSnapshot:
        """强制从磁盘重载快照（用于外部处置坏档后）。"""
        self._snapshot = self._catalog.load_snapshot()
        return self._snapshot

    def recover(self) -> CatalogSnapshot:
        """坏 current 且备份可用 → 恢复；否则原样 DATA_CORRUPT（不建空库）。"""
        self._snapshot = self._catalog.recover()
        return self._snapshot

    def is_referenced(self, entity_id: str) -> bool:
        """活动引用查询端口（Q2：默认「无活动引用」；TASK-022 落地）。"""
        if self._reference_port is None:
            return False
        if callable(self._reference_port):
            return bool(self._reference_port(entity_id))
        return bool(self._reference_port.is_referenced(entity_id))

    # -- 写 ------------------------------------------------------------------

    def commit(self, change: CatalogChange, expected_generation: Optional[str]) -> CommitResult:
        """按 S0–S11 提交一个 generation。

        Args:
            change: 不可变变更值（``CatalogChange``）。
            expected_generation: 提交者所见 current generation；None 表示预期为空。

        Raises:
            StorageError: CONFLICT（generation/revision）/ DATA_CORRUPT（校验）/
                RESOURCE_LIMIT（数量）/ STORAGE_ERROR（IO/第二写者）。
        """
        if not isinstance(change, CatalogChange):
            raise invalid_params("change", "change 必须为 CatalogChange")
        with self._lock:
            # S1：读 current + 冲突校验（实体 revision 先于 generation，Q5）
            current = self._catalog.read_current()
            current_gen = current["generation"] if current else None
            snapshot = self._snapshot if self._snapshot is not None else self._catalog.load_snapshot()
            if change.expected_revisions:
                check_expected_revisions(snapshot, change.expected_revisions)
            if expected_generation != current_gen:
                raise conflict(
                    "expected_generation 不符（当前 %r，期望 %r）"
                    % (current_gen, expected_generation),
                    field="expected_generation",
                )

            # S2：序列化新快照（三 JSON）+ 新 generation id
            targets = [dict(item) for item in change.targets]
            groups = [dict(item) for item in change.groups]
            next_sample_no = self._resolve_next_sample_no(snapshot, change, targets)
            script_doc = generation.script_metadata_document(change.script_metadata)

            # S3：写新资产（内容寻址、不可变）
            for asset_write in change.assets:
                self._images.persist(asset_write)

            # S7a：校验实体/FK/资产哈希（失败 → DATA_CORRUPT，旧档完好、不写 generation）
            validate_entities(targets, groups)
            self._verify_target_assets(targets)

            targets_doc = generation.targets_document(targets, next_sample_no)
            groups_doc = generation.groups_document(groups)
            raw_targets = generation.encode_json(targets_doc)
            raw_groups = generation.encode_json(groups_doc)
            raw_script = generation.encode_json(script_doc)

            new_generation = generation.new_generation_id()
            counts = {
                "targets": len(targets),
                "groups": len(groups),
                "scripts": len(script_doc.get("scripts", [])),
                "next_sample_no": next_sample_no,
            }
            manifest = generation.build_manifest(
                new_generation,
                (
                    (TARGETS_NAME, raw_targets),
                    (GROUPS_NAME, raw_groups),
                    (SCRIPT_METADATA_NAME, raw_script),
                ),
                counts,
                self._clock(),
            )
            manifest_bytes = generation.encode_manifest(manifest)

            # S4/S5/S6：写 generation 目录（原子写 + fsync）
            self._catalog.write_generation(
                new_generation,
                {
                    TARGETS_NAME: raw_targets,
                    GROUPS_NAME: raw_groups,
                    SCRIPT_METADATA_NAME: raw_script,
                },
            )
            self._catalog.write_manifest(new_generation, manifest_bytes)

            # S7b：回读校验文件 hash/字节与 manifest 一致
            self._catalog.verify_generation(new_generation, manifest)

            # S8/S9：同卷临时写 current 指针 → 原子替换（占用有限重试）
            self._catalog.backup_current()
            current_doc = {
                "generation": new_generation,
                "manifest_sha256": generation.manifest_sha256(manifest_bytes),
            }
            self._catalog.activate(current_doc)

            # S10：更新内存快照指针（读者经此见完整新版本）
            self._snapshot = CatalogSnapshot(
                generation=new_generation,
                manifest=manifest,
                targets=tuple(targets),
                groups=tuple(groups),
                script_metadata=script_doc,
                next_sample_no=next_sample_no,
            )
            self._log("generation 已提交: %s" % new_generation)

        # S11：返回 CommitResult
        return CommitResult(
            generation=current_doc["generation"],
            manifest_sha256=current_doc["manifest_sha256"],
        )

    # -- 内部校验 ------------------------------------------------------------

    def _resolve_next_sample_no(
        self,
        snapshot: CatalogSnapshot,
        change: CatalogChange,
        targets: Any,
    ) -> int:
        max_sample = max((int(item["sample_no"]) for item in targets), default=0)
        previous = int(snapshot.next_sample_no or 0)
        if change.next_sample_no is not None:
            candidate = int(change.next_sample_no)
            if candidate <= max_sample:
                raise data_corrupt(
                    "next_sample_no(%d) 必须大于已有最大 sample_no(%d)"
                    % (candidate, max_sample),
                    field="next_sample_no",
                )
            if candidate < previous:
                raise data_corrupt(
                    "next_sample_no 不得回退（%d < %d）" % (candidate, previous),
                    field="next_sample_no",
                )
            return candidate
        # 删除不回退：取旧值与「最大编号+1」的较大者
        return max(previous, max_sample + 1)

    def _verify_target_assets(self, targets: Any) -> None:
        for target in targets:
            try:
                asset: ImageAsset = validate_image_asset(target.get("image"))
            except TypeValidationError as exc:
                raise data_corrupt("目标 image 非法: %s" % exc.message, field=exc.field) from exc
            self._images.verify(asset)

    def close(self) -> None:
        """释放资源（会话级 ImageStore）。"""
        self._images.close()
