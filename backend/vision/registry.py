# -*- coding: utf-8 -*-
"""Provider 注册表与装配入口（TASK-013 §5）。

- ``BUILTIN_PROVIDER_FACTORIES``：内置工厂表（**基座为空**；TASK-014/015 追加）；
- ``register_builtin_provider``：注册内置工厂（重复 provider_id → ``ValueError``，防静默覆盖）；
- ``ProviderRegistry``：实例化 + 按条件类型查找 + 能力态 + 模型清单 + ``close_all``；
- ``build_default_vision_service``：唯一装配入口（复用共享 ``Repository``/``image_store``）。

**Q10**：同一条件类型只允许**唯一** provider；重复声明在 ``initialize_all`` **装配期明确失败**
（不静默）。未通过 api_version/初始化/缺模型的 provider **不注册到条件类型表**，能力置
``unavailable``（带 reason）。

**Q1**：本模块**不改** ``backend/ipc/jobs.py``；在途上限由 ``VisionService`` 自记账。
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..types.capabilities import ModelManifest, ProviderManifest
from .manifest import (
    check_api_version,
    load_model_manifest,
    model_manifest_path,
    models_dir_for,
    validate_provider,
)
from .ports import (
    IdentityFrameResolver,
    PixelReader,
    ProviderFactory,
    RecognitionProvider,
    RepositoryTargetSnapshotProvider,
    SessionPixelReader,
    StoreFrameResolver,
)
from .providers import ColorProvider, ContourProvider, OCREngineProvider, TemplateProvider
from .providers.ocr_models import resolve_models_dir
from .types import RecognitionError

#: 条件类型 → 能力 id 映射（对齐 ``types.capabilities.CAPABILITY_IDS``）。
CAPABILITY_CONDITION_TYPES: Dict[str, str] = {
    "ocr": "ocr",
    "template_match": "template",
    "contour_match": "contour",
    "color_match": "color",
}

#: 内置工厂表（基座为空；``register_builtin_provider`` 追加）。
BUILTIN_PROVIDER_FACTORIES: Dict[str, ProviderFactory] = {}


def register_builtin_provider(provider_id: str, factory: ProviderFactory) -> None:
    """注册内置工厂；重复 ``provider_id`` → ``ValueError``（防静默覆盖）。"""
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("provider_id 必须为非空字符串")
    if provider_id in BUILTIN_PROVIDER_FACTORIES:
        raise ValueError("重复注册内置 Provider 工厂: %s" % provider_id)
    BUILTIN_PROVIDER_FACTORIES[provider_id] = factory


# TASK-014：预置三算法 Provider 工厂（template/contour/color）。
# 每个只声明**唯一**条件类型（Q10）→ 装配期无冲突；缺 cv2/numpy 时
# ``initialize`` 抛 ``ENGINE_UNAVAILABLE`` → 能力诚实置 unavailable+reason。
register_builtin_provider("builtin.template", lambda pixels: TemplateProvider(pixels))
register_builtin_provider("builtin.contour", lambda pixels: ContourProvider(pixels))
register_builtin_provider("builtin.color", lambda pixels: ColorProvider(pixels))

# TASK-015：第 4 个内置 Provider —— OCR 离线引擎（`condition_type=ocr`）。
# **加法式**：不改 `BUILTIN_PROVIDER_FACTORIES` 语义 / `_condition_owner` 唯一性 /
# `initialize_all`/`capability_reason`/`unavailable_code`。缺 paddleocr 时 `initialize`
# 抛 `ENGINE_UNAVAILABLE` → 能力诚实置 unavailable+reason（受管 Python 分支）。
register_builtin_provider("builtin.ocr", lambda pixels: OCREngineProvider(pixels))


class ProviderRegistry:
    """Provider 注册表（内置工厂 → 实例化 → 校验 → 就绪/不可用）。

    Attributes:
        _factories: ``provider_id → ProviderFactory``。
        _ready: ``provider_id → (provider, ProviderManifest)``（就绪）。
        _condition_owner: ``condition_type → provider_id``（**唯一** provider，Q10）。
        _unavailable: ``provider_id → (manifest|None, reason)``。
    """

    def __init__(
        self,
        pixel_reader: Any,
        *,
        factories: Optional[Dict[str, ProviderFactory]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._pixel_reader = pixel_reader
        self._logger = logger
        self._factories: Dict[str, ProviderFactory] = dict(
            factories if factories is not None else BUILTIN_PROVIDER_FACTORIES
        )
        self._ready: Dict[str, Tuple[Any, ProviderManifest]] = {}
        self._condition_owner: Dict[str, str] = {}
        self._unavailable: Dict[str, Tuple[Optional[ProviderManifest], str]] = {}
        # TASK-014：初始化失败时记录的 06 错误码（如 ENGINE_UNAVAILABLE），
        # 供 service 区分「引擎不具备」（ENGINE_UNAVAILABLE）与「未装配」
        # （CAPABILITY_DENIED）。**加法式**，不改既有结构。
        self._unavailable_codes: Dict[str, str] = {}
        self._model_manifests: List[ModelManifest] = []
        self._locks: Dict[str, threading.Lock] = {}
        self._closed = False

    # -- 访问器 --------------------------------------------------------------

    @property
    def factories(self) -> Dict[str, ProviderFactory]:
        return dict(self._factories)

    @property
    def ready_providers(self) -> Dict[str, Any]:
        return {pid: provider for pid, (provider, _m) in self._ready.items()}

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    # -- 注册 ----------------------------------------------------------------

    def register_factory(self, provider_id: str, factory: ProviderFactory) -> None:
        """写入工厂表（重复 → ``ValueError``）。"""
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("provider_id 必须为非空字符串")
        if provider_id in self._factories:
            raise ValueError("重复注册 Provider 工厂: %s" % provider_id)
        self._factories[provider_id] = factory

    def initialize_all(self, models_dir: str) -> None:
        """对每个工厂：实例化 → ``capabilities`` → 校验 → api_version → ``initialize``。

        任一步失败仅记诊断并把该 provider 置不可用（**不阻断引擎启动**）；但**重复条件类型**
        （Q10）在装配期**明确失败**。
        """
        for provider_id, factory in self._factories.items():
            self._initialize_one(provider_id, factory, models_dir)

    def _initialize_one(self, provider_id: str, factory: ProviderFactory, models_dir: str) -> None:
        try:
            provider = factory(self._pixel_reader)
        except Exception as exc:  # 工厂构造失败：记诊断，置不可用
            self._unavailable[provider_id] = (None, "工厂构造失败: %s" % exc)
            self._log("vision provider %s 工厂构造失败：%s" % (provider_id, exc))
            return
        try:
            manifest = validate_provider(provider.capabilities())
        except RecognitionError as exc:
            self._unavailable[provider_id] = (None, exc.message)
            self._unavailable_codes[provider_id] = exc.code
            self._log("vision provider %s 能力清单非法：%s" % (provider_id, exc.message))
            return
        try:
            check_api_version(manifest)
        except RecognitionError as exc:
            self._unavailable[provider_id] = (manifest, exc.message)
            self._unavailable_codes[provider_id] = exc.code
            self._log("vision provider %s api_version 不受支持：%s" % (provider_id, exc.message))
            return
        try:
            provider.initialize(models_dir)
        except RecognitionError as exc:
            # 缺 cv2/numpy 等引擎缺失：记录 code（ENGINE_UNAVAILABLE），使 service
            # 在调用时抛 ENGINE_UNAVAILABLE 而非 CAPABILITY_DENIED（TASK-014 §6）。
            self._unavailable[provider_id] = (manifest, exc.message)
            self._unavailable_codes[provider_id] = exc.code
            self._log("vision provider %s 初始化失败（code=%s）：%s" % (provider_id, exc.code, exc.message))
            return
        except Exception as exc:  # 只读、无联网；缺模型/加载失败不致命
            self._unavailable[provider_id] = (manifest, "初始化失败: %s" % exc)
            self._log("vision provider %s 初始化失败：%s" % (provider_id, exc))
            return
        # 缺模型 → 该 provider 置不可用（07 §2；不抛致命错）
        models: List[ModelManifest] = []
        for model_id in manifest.model_ids:
            try:
                models.append(load_model_manifest(model_manifest_path(models_dir, model_id)))
            except RecognitionError as exc:
                self._unavailable[provider_id] = (manifest, exc.message)
                # Q2（ISSUE-TASK013-002）加法式修复：模型缺失分支**同样**记错误码，
                # 使 service 调用期抛 MODEL_UNAVAILABLE（而非 CAPABILITY_DENIED），
                # 与缺引擎分支（ENGINE_UNAVAILABLE）按原因分类一致。
                self._unavailable_codes[provider_id] = "MODEL_UNAVAILABLE"
                self._log("vision provider %s 模型 %s 不可用：%s" % (provider_id, model_id, exc.message))
                return
        # Q10：同一条件类型唯一 provider；重复声明装配期明确失败
        for condition_type in manifest.supported_condition_types:
            owner = self._condition_owner.get(condition_type)
            if owner is not None:
                raise ValueError(
                    "条件类型 %s 被多个就绪 Provider 声明：%s 与 %s" % (condition_type, owner, provider_id)
                )
        self._ready[provider_id] = (provider, manifest)
        self._model_manifests.extend(models)
        for condition_type in manifest.supported_condition_types:
            self._condition_owner[condition_type] = provider_id
        self._locks[provider_id] = threading.Lock()
        self._log("vision provider %s 就绪（条件类型=%s）"
                  % (provider_id, ",".join(manifest.supported_condition_types)))

    # -- 查询 ----------------------------------------------------------------

    def provider_for(self, condition_type: str) -> Optional[Any]:
        """按条件类型返回**唯一或首个**就绪 provider；无 → ``None``。"""
        provider_id = self._condition_owner.get(condition_type)
        if provider_id is None:
            return None
        return self._ready[provider_id][0]

    def manifest_for(self, provider: Any) -> Optional[ProviderManifest]:
        for _pid, (candidate, manifest) in self._ready.items():
            if candidate is provider:
                return manifest
        return None

    def serial_lock(self, provider: Any) -> threading.Lock:
        """``thread_safe=false`` 时串行化同一 provider 的并发调用（§7.3）。"""
        for pid, (candidate, _manifest) in self._ready.items():
            if candidate is provider:
                return self._locks.setdefault(pid, threading.Lock())
        return threading.Lock()

    def capability_reason(self, condition_type: str) -> Tuple[str, Optional[str]]:
        """能力态 ``(state, reason)``：就绪 → ``("available", None)``；否则 ``("unavailable", reason)``。"""
        provider_id = self._condition_owner.get(condition_type)
        if provider_id is not None:
            return ("available", None)
        for _pid, (manifest, reason) in self._unavailable.items():
            if manifest is not None and condition_type in manifest.supported_condition_types:
                return ("unavailable", reason)
        if not self._factories:
            return ("unavailable", "未装配识别 Provider")
        return ("unavailable", "无就绪 Provider 覆盖条件类型 %s" % condition_type)

    def unavailable_code(self, condition_type: str) -> Optional[str]:
        """声明 ``condition_type`` 但**不可用**的 Provider 的初始化失败码（TASK-014）。

        返回首个匹配不可用 Provider 记录的 06 错误码（如 ``ENGINE_UNAVAILABLE``）；无 → None。
        service 据此在「无就绪 Provider」时区分引擎缺失与未装配。
        """
        if condition_type in self._condition_owner:
            return None
        for provider_id, (manifest, _reason) in self._unavailable.items():
            if manifest is not None and condition_type in manifest.supported_condition_types:
                return self._unavailable_codes.get(provider_id)
        return None

    def installed_model_manifests(self) -> Tuple[ModelManifest, ...]:
        """供 ``hello.model_manifests``（基座为空元组）。"""
        return tuple(self._model_manifests)

    def close_all(self) -> None:
        """逐个 ``close()``（幂等；失败仅记诊断）。"""
        if self._closed:
            return
        self._closed = True
        for provider_id, (provider, _manifest) in self._ready.items():
            try:
                provider.close()
            except Exception as exc:  # 释放失败不影响其它 provider
                self._log("vision provider %s close 失败：%s" % (provider_id, exc))


# ---------------------------------------------------------------------------
# 装配入口（唯一）：复用共享 Repository / image_store
# ---------------------------------------------------------------------------

def build_default_vision_service(
    args: Any,
    *,
    repository: Any = None,
    image_store: Any = None,
    jobs: Any = None,
    logger: Optional[Callable[[str], None]] = None,
    pixel_reader: Any = None,
    target_snapshot_provider: Any = None,
    evaluator: Any = None,
    evaluator_factory: Any = None,
    frame_resolver: Any = None,
    registry: Optional[ProviderRegistry] = None,
    worker: Any = None,
    cache: Any = None,
    emitter: Any = None,
    monotonic: Optional[Callable[[], float]] = None,
    clock: Any = None,
    max_inflight: Optional[int] = None,
    deadline_ms: Optional[int] = None,
) -> Any:
    """装配 Vision 基座服务。

    **防脑裂（关键）**：复用 ``build_catalog_services`` 产出的**同一** ``Repository`` 与捕获/
    导入共享的**同一** ``image_store``（分别只读包装为 ``TargetSnapshotProvider``/``PixelReader``）；
    **不得**新建第二份。``initialize(model_dir)`` 传数据根 ``models/`` 的**绝对路径**（Q9）。
    """
    # 延迟 import 避免顶层循环（service 依赖 registry）。
    from .service import VisionService

    data_dir = getattr(args, "data_dir", None)
    if not data_dir:
        raise ValueError("build_default_vision_service 需要 args.data_dir")

    if pixel_reader is None:
        if image_store is None:
            raise ValueError("build_default_vision_service 需要 image_store 或 pixel_reader")
        pixel_reader = SessionPixelReader(image_store)
    if target_snapshot_provider is None and repository is not None:
        target_snapshot_provider = RepositoryTargetSnapshotProvider(repository, logger=logger)
    if frame_resolver is None:
        # TASK-014：默认用 StoreFrameResolver 读**同一** image_store 登记的帧元数据
        # （image_pixel/screen_physical 真实还原）；无 store → identity 诚实兜底。
        store = image_store if image_store is not None else getattr(pixel_reader, "_store", None)
        if store is not None:
            frame_resolver = StoreFrameResolver(store, clock=clock)
        else:
            frame_resolver = IdentityFrameResolver(clock=clock)

    reg = registry if registry is not None else ProviderRegistry(pixel_reader, logger=logger)
    if registry is None:
        # Q6：`models_dir` 解析优先 `<data_dir>/models/`，其次可配置模型库路径
        # （`--models-dir`/`ST_MODELS_DIR`）；来源记诊断（可审计）。**不下载/不联网**。
        models_dir, models_dir_source = resolve_models_dir(data_dir)
        if logger is not None:
            logger("vision models_dir=%s（来源=%s）" % (models_dir, models_dir_source))
        reg.initialize_all(models_dir)

    # TASK-016：条件树 evaluator 由组合根经 `evaluator_factory(reg)` 构造（需要**同一**
    # ProviderRegistry 实例做逐叶 provider 分派）；显式传入 `evaluator` 时优先。
    if evaluator is None and evaluator_factory is not None:
        evaluator = evaluator_factory(reg)

    kwargs: Dict[str, Any] = {
        "pixels": pixel_reader,
        "targets": target_snapshot_provider,
        "evaluator": evaluator,
        "frames": frame_resolver,
        "registry": reg,
        "logger": logger,
    }
    if jobs is not None:
        kwargs["jobs"] = jobs
    if worker is not None:
        kwargs["worker"] = worker
    if cache is not None:
        kwargs["cache"] = cache
    if emitter is not None:
        kwargs["emitter"] = emitter
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if clock is not None:
        kwargs["clock"] = clock
    if max_inflight is not None:
        kwargs["max_inflight"] = max_inflight
    if deadline_ms is not None:
        kwargs["deadline_ms"] = deadline_ms
    return VisionService(**kwargs)
