# -*- coding: utf-8 -*-
"""``ScriptService``（TASK-012）：编排「读快照 → 领域校验/变换 → 落源码 → 一致提交 → 发事件」。

职责边界（设计 §3/§4）：

- 读 ``Repository.read_snapshot()`` 得一致快照；调用纯领域函数（``scripts.domain``）做校验与
  元数据文档变换；源码经 ``ScriptSourceStore``（端口）内容寻址落盘；拼成一整套 ``CatalogChange``
  后一次 ``Repository.commit``；**仅当 commit 成功后**经 ``CatalogEmitter`` 发 ``catalog_changed``；
- **script revision 由本服务自查**（Q10：``CatalogSnapshot.revision_of``/``check_expected_revisions``
  只认 targets/groups，不认识 script）；对脚本传 ``expected_revisions={}``，
  generation 级并发冲突仍由 ``commit`` 的 ``expected_generation`` 兜底；
- **不含**存储 IO 细节（归 Repository/storage/sources）；**不**在保存路径做语法校验（Q2：保存不执行、
  不编译，语法检查只在 ``validate_script``）。

装配约束（设计 §15.2 防脑裂）：生产 ``main()`` 必须把**已构造的、与 ``TargetService`` 共享的**
``Repository`` 实例传给本服务——**不得**新建第二个 ``Repository``；``CatalogEmitter`` 亦共享同一实例。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..script.validator import ScriptValidator, build_default_script_validator
from ..storage.errors import data_corrupt, in_use, invalid_params
from ..storage.generation import sha256_hex
from ..targets.emitter import CatalogEmitter, NullCatalogEmitter
from ..targets.repository import CatalogChange, Repository
from . import domain
from .domain import UNSET
from .sources import ScriptSourceStore
from ..types.action_policy import validate_policy
from ..types.capabilities import TypeValidationError

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


class ScriptService:
    """脚本元数据/源码服务（端口：Repository、ScriptSourceStore、ScriptValidator、CatalogEmitter）。

    Attributes:
        repository: 与 ``TargetService`` **共享**的 ``Repository``（read_snapshot / commit /
            is_referenced / data_root）。
        source_store: 内容寻址源码存储端口。
        validator: 仅编译语法校验端口（``validate_script`` 用；保存路径不使用）。
        emitter: ``catalog_changed`` 事件端口（commit 成功后发；no-op/失败不发）。
    """

    def __init__(
        self,
        repository: Repository,
        source_store: ScriptSourceStore,
        validator: Optional[ScriptValidator] = None,
        emitter: Optional[CatalogEmitter] = None,
        *,
        clock: Optional[Clock] = None,
        id_gen: Optional[IdGen] = None,
    ) -> None:
        if repository is None:
            raise ValueError("repository 不能为空")
        if source_store is None:
            raise ValueError("source_store 不能为空")
        self._repository = repository
        self._source_store = source_store
        self._validator: ScriptValidator = (
            validator if validator is not None else build_default_script_validator()
        )
        self._emitter: CatalogEmitter = emitter if emitter is not None else NullCatalogEmitter()
        self._clock: Clock = clock if clock is not None else _default_clock
        self._id_gen: IdGen = id_gen if id_gen is not None else _default_id_gen

    # -- 访问器 --------------------------------------------------------------

    @property
    def repository(self) -> Repository:
        return self._repository

    @property
    def source_store(self) -> ScriptSourceStore:
        return self._source_store

    @property
    def validator(self) -> ScriptValidator:
        return self._validator

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
        """commit 成功后发 ``catalog_changed``（失败/no-op 路径不得调用本方法）。"""
        self._emitter.emit_catalog_changed(generation=generation, revision=int(revision))

    # -- 只读命令 ------------------------------------------------------------

    def list_scripts(
        self,
        offset: int = PAGE_DEFAULT_OFFSET,
        limit: int = PAGE_DEFAULT_LIMIT,
    ) -> Dict[str, Any]:
        """列表：``created_at`` 升序、tie-break ``id`` 升序（Q6）；分页返回 ``{items,total}``。"""
        snapshot = self._repository.read_snapshot()
        ordered: List[Mapping[str, Any]] = domain.sort_scripts(
            domain.scripts_from_snapshot(snapshot)
        )
        total = len(ordered)
        page = ordered[offset : offset + limit]
        return {"items": [dict(item) for item in page], "total": total}

    def load_script(self, script_id: str) -> Dict[str, Any]:
        """取脚本元数据 + 源码文本（回读并校验 hash；缺失/hash 不符 → ``DATA_CORRUPT``）。

        Raises:
            StorageError: SCRIPT_NOT_FOUND / DATA_CORRUPT。
        """
        snapshot = self._repository.read_snapshot()
        scripts = domain.scripts_from_snapshot(snapshot)
        script = domain.ensure_script_found(domain.find_script(scripts, script_id), script_id)
        return {"script": dict(script), "source": self.source_for(script)}

    def source_for(self, script) -> str:
        """Read and verify the immutable source named in a particular catalog generation."""
        raw = self._source_store.verify(
            str(script["source_path"]), str(script["source_sha256"])
        )
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:  # 源码资产非合法 UTF-8（不应发生，防御）
            raise data_corrupt(
                "脚本源码不是合法 UTF-8: %s" % script["source_path"], field="source"
            ) from exc
        return source

    # -- 写命令 --------------------------------------------------------------

    def save_script(
        self,
        name: Any,
        source: Any,
        parameters: Any,
        *,
        script_id: Any = UNSET,
        expected_revision: Any = UNSET,
        action_policy: Any = UNSET,
    ) -> Dict[str, Any]:
        """新建或更新脚本元数据 + 落源码（**不编译、不执行**，Q2）。

        新建/更新判定（03 §7.1）：「同时给 = 更新；同时省 = 新建；只给其一 → ``INVALID_PARAMS``」
        （``details.field`` = 缺失的伙伴字段，Q7）。

        同 ``(name, parameters, source_sha256)`` 重复保存 → **no-op**（Q3：不 commit、不换
        generation、不发事件，返回现状 Script）。

        Raises:
            StorageError: INVALID_PARAMS / RESOURCE_LIMIT / SCRIPT_NOT_FOUND / CONFLICT /
                STORAGE_ERROR。
        """
        has_id = script_id is not UNSET
        has_revision = expected_revision is not UNSET
        if has_id != has_revision:
            missing = "expected_revision" if not has_revision else "script_id"
            raise invalid_params(
                missing, "save_script 更新须同时提供 script_id 与 expected_revision"
            )

        snapshot = self._repository.read_snapshot()
        scripts = domain.scripts_from_snapshot(snapshot)
        normalized_name, source_bytes, normalized_params = domain.validate_script_input(
            name, source, parameters
        )
        source_sha256 = sha256_hex(source_bytes)
        if action_policy is not UNSET:
            try:
                validate_policy(action_policy)
            except TypeValidationError as exc:
                raise invalid_params(exc.field, exc.message) from exc

        existing: Optional[Mapping[str, Any]] = None
        if has_id:
            # 防御：命令层已用 _require_int 校验；此处再护一道，杜绝直接调用经
            # int() 抛 ValueError/TypeError（bool 亦拒绝，True/False 非 revision）。
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise invalid_params("expected_revision", "expected_revision 必须为整数")
            existing = domain.ensure_script_found(
                domain.find_script(scripts, script_id), str(script_id)
            )
            domain.ensure_script_revision(existing, expected_revision)
            if action_policy is UNSET:
                action_policy = existing.get("action_policy", UNSET)
            if (action_policy == existing.get("action_policy", UNSET)
                    and domain.is_noop_save(existing, normalized_name, normalized_params, source_sha256)):
                return dict(existing)  # no-op：不写盘、不 commit、不发事件

        now = self._clock()
        if existing is not None:
            effective_id = str(existing["id"])
            revision = int(existing["revision"]) + 1
            created_at = str(existing["created_at"])
        else:
            effective_id = self._id_gen()
            revision = 1
            created_at = now

        source_path = domain.source_relative_path(effective_id, source_sha256)
        self._source_store.persist(effective_id, source_bytes)
        script_dict = domain.build_script_dict(
            script_id=effective_id,
            revision=revision,
            name=normalized_name,
            source_path=source_path,
            source_sha256=source_sha256,
            parameters=normalized_params,
            created_at=created_at,
            updated_at=now,
        )
        if action_policy is not UNSET:
            script_dict["action_policy"] = action_policy
        new_scripts = domain.upsert_script(scripts, script_dict)
        change = CatalogChange.build(
            snapshot,
            targets=snapshot.targets,  # 原样透传（不变量）
            groups=snapshot.groups,  # 原样透传（不变量）
            script_metadata=domain.script_metadata_document(new_scripts),
            assets=(),  # 源码不是 ImageStore 资产
            expected_revisions={},  # script revision 由本服务自查（Q10）
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(script_dict["revision"]))
        return script_dict

    def delete_script(self, script_id: str, expected_revision: int) -> None:
        """删除脚本**元数据**（Q5：活动引用 → ``IN_USE``；**源码文件保留**，只保留不回收）。

        Raises:
            StorageError: SCRIPT_NOT_FOUND / CONFLICT / IN_USE / STORAGE_ERROR。
        """
        snapshot = self._repository.read_snapshot()
        scripts = domain.scripts_from_snapshot(snapshot)
        script = domain.ensure_script_found(domain.find_script(scripts, script_id), script_id)
        domain.ensure_script_revision(script, int(expected_revision))
        if self._repository.is_referenced(script_id):
            raise in_use(
                "脚本被活动执行引用，无法删除: %s" % script_id,
                details={"field": "script_id"},
            )
        new_scripts = domain.remove_script(scripts, script_id)
        change = CatalogChange.build(
            snapshot,
            targets=snapshot.targets,
            groups=snapshot.groups,
            script_metadata=domain.script_metadata_document(new_scripts),
            expected_revisions={},
        )
        result = self._repository.commit(change, snapshot.generation)
        self._emit(result.generation, int(expected_revision))

    # -- 语法校验（无状态、不读仓库、不落盘） --------------------------------

    def validate_script(self, source: Any) -> Dict[str, Any]:
        """**仅编译**语法校验：返回 ``{valid, diagnostics}``（``valid == (diagnostics==[])``）。

        ``source`` 先经领域校验（类型/UTF-8/预算）；lupa 缺失 → 抛 ``ENGINE_UNAVAILABLE``
        （Q1：``retryable=False``、无 details），**绝不**返回 ``valid:true``。

        Raises:
            StorageError: INVALID_PARAMS / RESOURCE_LIMIT（source）/ ENGINE_UNAVAILABLE。
        """
        domain.validate_source(source)
        diagnostics = self._validator.compile(source)
        payload = [diagnostic.to_dict() for diagnostic in diagnostics]
        return {"valid": len(payload) == 0, "diagnostics": payload}
