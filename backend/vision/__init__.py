# -*- coding: utf-8 -*-
"""``backend.vision`` barrel（TASK-013）：Vision Provider 基座。

- ``ports``：``RecognitionProvider``（逐字 00）+ ``PixelReader``/``TargetSnapshotProvider``/
  ``ConditionEvaluator``/``FrameResolver`` 端口 + 只读适配器；
- ``cancel``：取消原语（本地结构化定义，**不 import capture**）；
- ``types``：统一结果值对象（``Candidate``/``TextBlock``/``Match``/``RecognitionResult``…）+ 校验；
- ``nms``：IoU/NMS/100 上限纯函数；
- ``manifest``：``ProviderManifest``/``ModelManifest`` 加载与 api_version major 判定；
- ``cache``：识别结果 LRU 缓存；
- ``worker``：``WorkerRunner`` 端口 + 线程实现（超时/取消）；
- ``emitter``：``RecognitionEmitter`` 端口 + stdio 实现；
- ``registry``：Provider 注册表 + 内置工厂（TASK-015 追加 ``builtin.ocr``）+ ``build_default_vision_service``；
- ``service``：``VisionService``（job 化编排）；
- ``commands``：6 条识别命令接线；
- ``textmatch``：NFC/exact/contains/fuzzy 纯函数 + ``translate_polygon_to_space``（TASK-015 / TASK-016 复用）。

分层依赖单向：``commands → service → {registry, nms, types, cache, worker, cancel, emitter, ports}``；
隔离保证：Vision **不** import/inject Input；Provider 仅得只读 ``PixelReader``（无写/UI）。
"""

from __future__ import annotations

from .cache import DEFAULT_CACHE_CAPACITY, RecognitionCache, cache_key
from .cancel import (
    CancelFlag,
    CancelToken,
    CancelledError,
    JobBoundCancelToken,
    NullCancelToken,
)
from .commands import register_vision_commands
from .condition_evaluator import ConditionTreeEvaluator
from .condition_serializer import (
    LEAF_TYPES,
    BranchSpec,
    ConditionSpec,
    LeafSpec,
    leaf_roi_in_frame,
    serialize_condition_tree,
)
from .emitter import (
    NullRecognitionEmitter,
    RecognitionEmitter,
    RecordingRecognitionEmitter,
    StdioRecognitionEmitter,
)
from .manifest import (
    MODEL_MANIFEST_NAME,
    MODELS_DIRNAME,
    SUPPORTED_API_MAJOR,
    check_api_version,
    load_model_manifest,
    models_dir_for,
    validate_provider,
)
from .nms import IOU_THRESHOLD, MAX_CANDIDATES, cap_candidates, iou, nms
from .ports import (
    ConditionEvaluator,
    FrameResolver,
    IdentityFrameResolver,
    PixelReader,
    ProviderFactory,
    RawPixels,
    RecognitionProvider,
    RepositoryTargetSnapshotProvider,
    SessionPixelReader,
    StoreFrameResolver,
    TargetImageResolver,
    TargetSnapshot,
    TargetSnapshotProvider,
)
from .providers import (
    BaseAlgorithmProvider,
    BaseProvider,
    ColorProvider,
    ContourProvider,
    FakeProvider,
    NoopProvider,
    OCREngineProvider,
    OcrEngine,
    OcrModelCatalog,
    OcrModelEntry,
    OcrModelFile,
    TemplateProvider,
    format_ocr_reason,
    parse_ocr_reason,
    resolve_library_root,
    resolve_models_dir,
    seed_repo_model_metadata,
)
from .textmatch import (
    DEFAULT_FUZZY_THRESHOLD,
    DEFAULT_MIN_CONFIDENCE,
    MATCH_MODES,
    fuzzy_similarity,
    levenshtein,
    match_text,
    normalize_text,
    translate_polygon_to_space,
)
from .registry import (
    BUILTIN_PROVIDER_FACTORIES,
    CAPABILITY_CONDITION_TYPES,
    ProviderRegistry,
    build_default_vision_service,
    register_builtin_provider,
)
from .service import DEFAULT_MAX_INFLIGHT, VisionService
from .types import (
    Candidate,
    EvidenceLeaf,
    Match,
    ProviderResult,
    RecognitionError,
    RecognitionResult,
    TextBlock,
    build_recognition_result,
    center_of,
    validate_candidate,
    validate_recognition_result,
    validate_text_block,
)
from .worker import (
    INFERENCE_DEADLINE_MS,
    DeadlineCancelToken,
    ThreadInferenceWorker,
    WorkerRunner,
)

__all__ = [
    # cache
    "RecognitionCache",
    "cache_key",
    "DEFAULT_CACHE_CAPACITY",
    # cancel
    "CancelFlag",
    "CancelToken",
    "CancelledError",
    "JobBoundCancelToken",
    "NullCancelToken",
    # commands
    "register_vision_commands",
    # condition_serializer / condition_evaluator（TASK-016）
    "ConditionTreeEvaluator",
    "serialize_condition_tree",
    "leaf_roi_in_frame",
    "LeafSpec",
    "BranchSpec",
    "ConditionSpec",
    "LEAF_TYPES",
    # emitter
    "RecognitionEmitter",
    "StdioRecognitionEmitter",
    "NullRecognitionEmitter",
    "RecordingRecognitionEmitter",
    # manifest
    "SUPPORTED_API_MAJOR",
    "MODELS_DIRNAME",
    "MODEL_MANIFEST_NAME",
    "models_dir_for",
    "load_model_manifest",
    "validate_provider",
    "check_api_version",
    # nms
    "iou",
    "nms",
    "cap_candidates",
    "IOU_THRESHOLD",
    "MAX_CANDIDATES",
    # ports
    "RecognitionProvider",
    "ProviderFactory",
    "PixelReader",
    "RawPixels",
    "TargetSnapshotProvider",
    "TargetSnapshot",
    "TargetImageResolver",
    "ConditionEvaluator",
    "FrameResolver",
    "IdentityFrameResolver",
    "StoreFrameResolver",
    "SessionPixelReader",
    "RepositoryTargetSnapshotProvider",
    # providers
    "BaseProvider",
    "BaseAlgorithmProvider",
    "NoopProvider",
    "FakeProvider",
    "TemplateProvider",
    "ContourProvider",
    "ColorProvider",
    "OCREngineProvider",
    "OcrModelCatalog",
    "OcrModelEntry",
    "OcrModelFile",
    "OcrEngine",
    "format_ocr_reason",
    "parse_ocr_reason",
    "resolve_models_dir",
    "resolve_library_root",
    "seed_repo_model_metadata",
    # textmatch（TASK-015）
    "normalize_text",
    "levenshtein",
    "fuzzy_similarity",
    "match_text",
    "translate_polygon_to_space",
    "MATCH_MODES",
    "DEFAULT_FUZZY_THRESHOLD",
    "DEFAULT_MIN_CONFIDENCE",
    # registry
    "ProviderRegistry",
    "BUILTIN_PROVIDER_FACTORIES",
    "register_builtin_provider",
    "build_default_vision_service",
    "CAPABILITY_CONDITION_TYPES",
    # service
    "VisionService",
    "DEFAULT_MAX_INFLIGHT",
    # types
    "Candidate",
    "TextBlock",
    "EvidenceLeaf",
    "Match",
    "RecognitionResult",
    "ProviderResult",
    "RecognitionError",
    "center_of",
    "build_recognition_result",
    "validate_candidate",
    "validate_text_block",
    "validate_recognition_result",
    # worker
    "WorkerRunner",
    "ThreadInferenceWorker",
    "DeadlineCancelToken",
    "INFERENCE_DEADLINE_MS",
]
