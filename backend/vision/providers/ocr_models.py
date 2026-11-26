# -*- coding: utf-8 -*-
"""OCR 模型目录与离线能力元数据（TASK-015 T01；纯标准库，无 IO 副作用除只读）。

职责（分层：``providers/ocr.py → {ocr_models, types, ports, cancel}``）：

- ``OcrModelCatalog``：加载仓内权威目录 ``manifest/models.catalog.json``（已安装模型的
  **逐文件 sha256 + 许可证 + 语言**；权重二进制**不入仓库**，留在 ``~/.paddlex/official_models``），
  提供 ``installed_model_ids``/``language_of``/``model_ids_for_language``/``verify``；
- 目录/模型库解析（Q6）：``resolve_models_dir``（优先 ``<data_dir>/models/``，其次
  ``--models-dir``/``ST_MODELS_DIR``）、``resolve_library_root``（权重库根，``ST_OCR_MODELS_DIR``
  可覆盖，缺省 ``~/.paddlex/official_models``）；**禁止下载/联网**；
- ``seed_repo_model_metadata``：把仓内 ``models/<id>/manifest.json``（**仅元数据**）落到
  目标 ``models_dir``，使 ``registry.load_model_manifest`` 可读到「已安装模型」（幂等，不覆盖）；
- ``format_ocr_reason``/``parse_ocr_reason``：``capabilities[ocr].reason`` 的**固定规范格式**
  （Q8；写入 ``docs/03`` 并被单测锁定）。

**顶层零重依赖**：本模块模块顶层只 import 标准库与 ``backend.*`` 纯 Python 模块。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...config.service import ConfigService
from ...storage.errors import StorageError
from ...storage.fsops import write_atomic_bytes
from ...storage.paths import DataRoot
from ...types.capabilities import ModelManifest, validate_model_manifest
from ..types import RecognitionError

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本模块所在仓库根（``backend/vision/providers/ocr_models.py`` → 上溯 3 级）。
REPO_ROOT: str = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

#: 仓内权威模型目录（只读元数据）。
REPO_CATALOG_PATH: str = os.path.join(REPO_ROOT, "manifest", "models.catalog.json")
#: 仓内 02 布局模型 manifest 根（``models/<model_id>/manifest.json``）。
REPO_MODELS_DIR: str = os.path.join(REPO_ROOT, "models")

#: 权重库根（模型二进制所在，不入仓库）。
DEFAULT_LIBRARY_ROOTNAME: str = "official_models"
PADDLEX_DIRNAME: str = ".paddlex"

#: 环境变量（Q6：可配置模型库路径；本任务**只读本地，不下载**）。
ENV_LIBRARY_ROOT: str = "ST_OCR_MODELS_DIR"
ENV_MODELS_DIR: str = "ST_MODELS_DIR"

#: 单模型 manifest 文件名（02 磁盘布局）。
MODEL_MANIFEST_NAME: str = "manifest.json"

# Previous unmodified project metadata, normalized to LF for Windows and Git checkouts.
_PREVIOUS_METADATA = {
    "PP-OCRv6_medium_rec": "552be2788d627710d2795bf3b21908429c65c7052fc16b82123a31d55890c6c6",
}

#: ``capabilities[ocr].reason`` 固定规范格式（Q8）的字段顺序。
REASON_PREFIX: str = "OCR"
REASON_FIELD_ORDER: Tuple[str, ...] = (
    "state", "code", "models", "verified", "unverified", "models_dir", "library", "detail",
)
#: 空值占位符（未安装/无实证项）。
REASON_EMPTY: str = "-"


def default_library_root() -> str:
    """缺省权重库根：``~/.paddlex/official_models``。"""
    return os.path.join(os.path.expanduser("~"), PADDLEX_DIRNAME, DEFAULT_LIBRARY_ROOTNAME)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OcrModelFile:
    """模型文件条目（逐文件 sha256，来自仓内目录）。"""

    name: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class OcrModelEntry:
    """一个已安装模型的元数据（**不含权重**）。"""

    model_id: str
    family: str
    kind: str
    languages: Tuple[str, ...]
    primary_language: Optional[str]
    license: str
    source: str
    files: Tuple[OcrModelFile, ...]


@dataclass(frozen=True)
class OcrEngine:
    """识别引擎配对（检测模型 + 识别模型）。"""

    detection_model: str
    recognition_model: str
    languages: Tuple[str, ...]


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------

class OcrModelCatalog:
    """仓内「已安装模型」目录（纯标准库；只读 JSON，无网络）。"""

    def __init__(
        self,
        entries: Tuple[OcrModelEntry, ...],
        engines: Tuple[OcrEngine, ...],
        first_batch_languages: Tuple[str, ...],
    ) -> None:
        self._entries = entries
        self._by_id: Dict[str, OcrModelEntry] = {entry.model_id: entry for entry in entries}
        self._engines = engines
        self._engine_by_rec: Dict[str, OcrEngine] = {
            engine.recognition_model: engine for engine in engines
        }
        self._first_batch_languages = first_batch_languages

    # -- 加载 ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "OcrModelCatalog":
        """读取并校验仓内目录；格式非法 → ``MODEL_UNAVAILABLE``（不静默）。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise RecognitionError("MODEL_UNAVAILABLE", "模型目录不可读: %s" % path,
                                   retryable=False) from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RecognitionError("MODEL_UNAVAILABLE", "模型目录非合法 JSON: %s" % path,
                                   retryable=False) from exc
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise RecognitionError("MODEL_UNAVAILABLE", "模型目录结构非法（缺 models 数组）",
                                   retryable=False)
        entries: List[OcrModelEntry] = []
        for raw in data["models"]:
            entries.append(cls._entry(raw))
        engines: List[OcrEngine] = []
        for raw in data.get("engines", []) or []:
            engines.append(
                OcrEngine(
                    detection_model=str(raw.get("detection_model") or ""),
                    recognition_model=str(raw.get("recognition_model") or ""),
                    languages=tuple(str(item) for item in (raw.get("languages") or [])),
                )
            )
        first_batch = tuple(str(item) for item in (data.get("first_batch_languages") or []))
        return cls(tuple(entries), tuple(engines), first_batch)

    @staticmethod
    def _entry(raw: Any) -> OcrModelEntry:
        if not isinstance(raw, dict) or not raw.get("model_id"):
            raise RecognitionError("MODEL_UNAVAILABLE", "模型目录条目非法（缺 model_id）",
                                   retryable=False)
        files: List[OcrModelFile] = []
        for item in raw.get("files", []) or []:
            files.append(
                OcrModelFile(
                    name=str(item.get("name") or ""),
                    bytes=int(item.get("bytes") or 0),
                    sha256=str(item.get("sha256") or ""),
                )
            )
        languages = tuple(str(item) for item in (raw.get("languages") or []))
        primary = raw.get("primary_language")
        return OcrModelEntry(
            model_id=str(raw["model_id"]),
            family=str(raw.get("family") or ""),
            kind=str(raw.get("kind") or ""),
            languages=languages,
            primary_language=(str(primary) if primary else None),
            license=str(raw.get("license") or ""),
            source=str(raw.get("source") or ""),
            files=tuple(files),
        )

    # -- 访问器 -------------------------------------------------------------

    @property
    def entries(self) -> Tuple[OcrModelEntry, ...]:
        return self._entries

    @property
    def engines(self) -> Tuple[OcrEngine, ...]:
        return self._engines

    @property
    def first_batch_languages(self) -> Tuple[str, ...]:
        return self._first_batch_languages

    def entry(self, model_id: str) -> Optional[OcrModelEntry]:
        return self._by_id.get(model_id)

    def installed_model_ids(self) -> Tuple[str, ...]:
        return tuple(entry.model_id for entry in self._entries)

    def recognition_models(self) -> Tuple[str, ...]:
        """可经 ``recognize_text{model_id}`` 请求的识别模型（引擎的识别端）。"""
        return tuple(engine.recognition_model for engine in self._engines)

    def required_model_ids(self) -> Tuple[str, ...]:
        """引擎运行所需模型（检测 + 识别）去重有序。"""
        needed: List[str] = []
        for engine in self._engines:
            for model_id in (engine.detection_model, engine.recognition_model):
                if model_id and model_id not in needed:
                    needed.append(model_id)
        return tuple(needed)

    def engine_for(self, recognition_model: str) -> Optional[OcrEngine]:
        return self._engine_by_rec.get(recognition_model)

    def language_of(self, model_id: str) -> Optional[str]:
        """模型身份语言（``primary_language``）；通用多语模型无单一语言 → ``None``（不编造）。"""
        entry = self._by_id.get(model_id)
        return entry.primary_language if entry is not None else None

    def model_ids_for_language(self, language: str) -> Tuple[str, ...]:
        """声明覆盖 ``language`` 的识别模型（实证）。"""
        return tuple(
            engine.recognition_model for engine in self._engines
            if language in engine.languages
        )

    def supported_languages(self) -> Tuple[str, ...]:
        """全部识别引擎实证语言（去重、保持引擎声明顺序）。"""
        ordered: List[str] = []
        for engine in self._engines:
            for language in engine.languages:
                if language not in ordered:
                    ordered.append(language)
        return tuple(ordered)

    def unverified_languages(self) -> Tuple[str, ...]:
        """首批声明语言中**未实证**者（如实披露；不宣称支持）。"""
        verified = set(self.supported_languages())
        return tuple(lang for lang in self._first_batch_languages if lang not in verified)

    # -- 校验 ---------------------------------------------------------------

    def verify(self, model_root: str, model_ids: Optional[Tuple[str, ...]] = None) -> None:
        """逐文件 sha256 校验（缺文件 → ``MODEL_UNAVAILABLE``；不符 → ``DATA_CORRUPT``）。

        Args:
            model_root: 权重库根（其下为 ``<model_id>/<file>``）。
            model_ids: 待校验模型；``None`` → 引擎所需模型（检测 + 识别）。
        """
        if not isinstance(model_root, str) or not model_root:
            raise RecognitionError("MODEL_UNAVAILABLE", "模型库根为空", retryable=False)
        targets = model_ids if model_ids is not None else self.required_model_ids()
        for model_id in targets:
            entry = self._by_id.get(model_id)
            if entry is None:
                raise RecognitionError("MODEL_UNAVAILABLE", "模型不在目录中: %s" % model_id,
                                       retryable=False)
            for item in entry.files:
                try:
                    path = DataRoot(model_root).resolve(os.path.join(model_id, item.name))
                except StorageError as exc:
                    raise RecognitionError("MODEL_UNAVAILABLE", exc.message) from exc
                if not os.path.isfile(path):
                    raise RecognitionError(
                        "MODEL_UNAVAILABLE", "模型文件缺失: %s" % path, retryable=False
                    )
                if item.sha256 and _sha256_file(path) != item.sha256:
                    raise RecognitionError(
                        "DATA_CORRUPT", "模型文件 sha256 不符: %s" % path, retryable=False
                    )

    def weight_sha256(self, model_id: str) -> Optional[str]:
        """主权重文件（``inference.pdiparams``，缺则 ``inference.json``）的 sha256。"""
        entry = self._by_id.get(model_id)
        if entry is None:
            return None
        for preferred in ("inference.pdiparams", "inference.json"):
            for item in entry.files:
                if item.name == preferred:
                    return item.sha256
        return None


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 目录/模型库解析 + 元数据 seed（Q6）
# ---------------------------------------------------------------------------

def resolve_models_dir(data_dir: str, override: Optional[str] = None, *,
                       config_service: Optional[ConfigService] = None) -> Tuple[str, str]:
    """解析 ``models_dir``（供 registry 读 ``models/<id>/manifest.json``）。

    显式调用参数优先，其次为设置页保存的非默认数据根相对目录。
    默认目录兼容 ST_MODELS_DIR 及随包权重路径。
    """
    if override:
        return os.path.abspath(override), "config"
    if data_dir:
        # Startup recovery belongs to the same service backing the UI's get_config.
        settings = config_service if config_service is not None else ConfigService(data_dir)
        if os.path.isfile(settings.config_path):
            directory = settings.load()["recognition"]["model_directory"]
            if directory != "models":
                return DataRoot(data_dir).resolve(directory), "settings"
    env_dir = os.environ.get(ENV_MODELS_DIR)
    if env_dir:
        return os.path.abspath(env_dir), "config"
    if not data_dir:
        raise ValueError("resolve_models_dir 需要 data_dir 或 override")
    return DataRoot(data_dir).resolve("models"), "data_dir_default"


def resolve_library_root(override: Optional[str] = None) -> Tuple[str, str]:
    """解析权重库根（模型二进制所在）。

    返回 ``(绝对路径, 来源标记)``；来源 ∈ ``{"config", "default"}``（可审计）。
    """
    chosen = override if override else os.environ.get(ENV_LIBRARY_ROOT)
    if chosen:
        return os.path.abspath(chosen), "config"
    return os.path.abspath(default_library_root()), "default"


def seed_repo_model_metadata(models_dir: str) -> List[str]:
    """把仓内 ``models/<id>/manifest.json``（**仅元数据**）落到目标 ``models_dir``。

    幂等：仅更新与旧版出厂哈希相符的元数据，保留用户文件。返回本次写入的 model_id。
    失败（权限/IO）静默跳过——**不阻断**引擎启动；调用方以返回值为准（可审计）。
    """
    if not isinstance(models_dir, str) or not models_dir:
        return []
    seeded: List[str] = []
    if not os.path.isdir(REPO_MODELS_DIR):
        return seeded
    for model_id in sorted(os.listdir(REPO_MODELS_DIR)):
        source = os.path.join(REPO_MODELS_DIR, model_id, MODEL_MANIFEST_NAME)
        if not os.path.isfile(source):
            continue
        try:
            target = DataRoot(models_dir).resolve(os.path.join(model_id, MODEL_MANIFEST_NAME))
            if os.path.isfile(target):
                previous = _PREVIOUS_METADATA.get(model_id)
                if not previous or os.path.islink(target):
                    continue
                data = Path(target).read_bytes().replace(b"\r\n", b"\n")
                if hashlib.sha256(data).hexdigest() != previous:
                    continue
            write_atomic_bytes(target, Path(source).read_bytes())
        except (OSError, StorageError):
            continue
        seeded.append(model_id)
    return seeded


def load_repo_model_manifest(model_id: str) -> Optional[ModelManifest]:
    """读取仓内 ``models/<id>/manifest.json`` 并校验（供单测/诊断；只读）。"""
    path = os.path.join(REPO_MODELS_DIR, model_id, MODEL_MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError):
        return None
    try:
        return validate_model_manifest(data)
    except Exception:  # noqa: BLE001 - 诊断用途，非法即 None
        return None


# ---------------------------------------------------------------------------
# capabilities[ocr].reason 固定规范格式（Q8；docs/03 锁定）
# ---------------------------------------------------------------------------

def _sanitize(value: Any) -> str:
    """把字段值规范为单行、无 ``|`` 的字符串（保障格式可解析）。"""
    return " ".join(str(value).replace("|", "/").split())


def _join(items: Any) -> str:
    values = [str(item) for item in (items or ()) if str(item)]
    return ",".join(values) if values else REASON_EMPTY


def format_ocr_reason(
    *,
    state: str,
    code: Optional[str] = None,
    models: Any = (),
    verified: Any = (),
    unverified: Any = (),
    models_dir: Optional[str] = None,
    library: Optional[str] = None,
    detail: str = "",
) -> str:
    """构造 ``capabilities[ocr].reason`` 的**固定规范格式**（Q8）。

    格式：``OCR|state=<..>|code=<..>|models=<csv|->|verified=<csv|->|unverified=<csv|->|
    models_dir=<..|->|library=<..|->|detail=<text>``（字段顺序见 ``REASON_FIELD_ORDER``）。
    """
    values = {
        "state": _sanitize(state) or REASON_EMPTY,
        "code": _sanitize(code) if code else REASON_EMPTY,
        "models": _join(models),
        "verified": _join(verified),
        "unverified": _join(unverified),
        "models_dir": _sanitize(models_dir) if models_dir else REASON_EMPTY,
        "library": _sanitize(library) if library else REASON_EMPTY,
        "detail": _sanitize(detail),
    }
    parts = [REASON_PREFIX]
    for field in REASON_FIELD_ORDER:
        parts.append("%s=%s" % (field, values[field]))
    return "|".join(parts)


def parse_ocr_reason(reason: Optional[str]) -> Optional[Dict[str, Any]]:
    """解析 ``capabilities[ocr].reason``（语义注记；非公开契约）。

    非 ``OCR|`` 前缀/结构非法 → ``None``（调用方按「无结构化信息」处理）。
    """
    if not isinstance(reason, str) or not reason.startswith(REASON_PREFIX + "|"):
        return None
    chunks = reason.split("|")[1:]
    parsed: Dict[str, Any] = {}
    for chunk in chunks:
        key, sep, value = chunk.partition("=")
        if not sep or key not in REASON_FIELD_ORDER:
            return None
        parsed[key] = value
    if set(parsed) != set(REASON_FIELD_ORDER):
        return None
    for key in ("models", "verified", "unverified"):
        parsed[key] = [] if parsed[key] == REASON_EMPTY else parsed[key].split(",")
    for key in ("code", "models_dir", "library"):
        if parsed[key] == REASON_EMPTY:
            parsed[key] = None
    return parsed
