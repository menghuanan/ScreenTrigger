# -*- coding: utf-8 -*-
"""Provider/Model manifest 加载与 api_version 判定（TASK-013 §4.2；纯函数 + 只读 IO）。

- ``validate_provider(value)``：复用 ``types.capabilities.validate_provider_manifest``（**不复制**字段规则）；
- ``load_model_manifest(path)``：从 ``models/<id>/manifest.json`` 读取并校验（**只读、禁联网**）；
- ``check_api_version(manifest)``：**未知 major 拒绝初始化**（Q5：``ENGINE_UNAVAILABLE``，``retryable=false``，
  ``details.field="provider_manifest.api_version"``）。

首版 ``SUPPORTED_API_MAJOR = 1``（``api_version`` 即 major，无 minor 编码）。缺模型不抛致命错：
``load_model_manifest`` 失败以 ``MODEL_UNAVAILABLE`` 表达，调用方（注册表）据此置该 provider 不可用。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..types.capabilities import (
    ModelManifest,
    ProviderManifest,
    TypeValidationError,
    validate_model_manifest,
    validate_provider_manifest,
)
from .types import RecognitionError

#: 数据根下模型目录名与 manifest 文件名（02 磁盘布局）。
MODELS_DIRNAME: str = "models"
MODEL_MANIFEST_NAME: str = "manifest.json"

#: 首版冻结的 API major（00：未知 major 拒绝初始化）。
SUPPORTED_API_MAJOR: int = 1


def engine_unavailable(message: str, field: str | None = None) -> RecognitionError:
    """``ENGINE_UNAVAILABLE``（引擎侧不具备该能力；retryable=false）。"""
    return RecognitionError("ENGINE_UNAVAILABLE", message, field=field, retryable=False)


def model_unavailable(message: str, field: str | None = None) -> RecognitionError:
    """``MODEL_UNAVAILABLE``（provider/模型当前不可加载；retryable=false）。"""
    return RecognitionError("MODEL_UNAVAILABLE", message, field=field, retryable=False)


def models_dir_for(data_dir: str) -> str:
    """数据根下 ``models/`` 的**绝对路径**（Q9：initialize 传此路径）。"""
    if not isinstance(data_dir, str) or not data_dir:
        raise ValueError("data_dir 必须为非空字符串")
    return os.path.join(os.path.abspath(data_dir), MODELS_DIRNAME)


def model_manifest_path(models_dir: str, model_id: str) -> str:
    """``models/<model_id>/manifest.json`` 的绝对路径。"""
    from ..storage.paths import DataRoot
    from ..storage.errors import StorageError
    try:
        return DataRoot(models_dir).resolve(os.path.join(model_id, MODEL_MANIFEST_NAME))
    except StorageError as exc:
        raise RecognitionError("MODEL_UNAVAILABLE", exc.message) from exc


def validate_provider(value: Any) -> ProviderManifest:
    """校验 ``ProviderManifest``（接受 dict 或已构造的 ``ProviderManifest``）。

    - dict → 复用三端同源校验器 ``validate_provider_manifest``；
    - 已是 ``ProviderManifest`` → 原样返回（``provider.capabilities()`` 直接产出）。
    错误归一为 ``RecognitionError``。
    """
    if isinstance(value, ProviderManifest):
        return value
    try:
        return validate_provider_manifest(value)
    except TypeValidationError as exc:
        raise RecognitionError(exc.code, exc.message, field=exc.field,
                               retryable=exc.retryable) from exc


def check_api_version(manifest: ProviderManifest) -> None:
    """api_version major 判定：未知 major → ``ENGINE_UNAVAILABLE``（Q5）。"""
    major = int(manifest.api_version)
    if major != SUPPORTED_API_MAJOR:
        raise engine_unavailable(
            "Provider API major 不受支持：声明 %r，支持 %d" % (manifest.api_version, SUPPORTED_API_MAJOR),
            field="provider_manifest.api_version",
        )


def load_model_manifest(path: str) -> ModelManifest:
    """读取并校验 ``models/<id>/manifest.json``（**只读、禁联网**）。

    Raises:
        RecognitionError: MODEL_UNAVAILABLE（文件不可读/非 JSON）或字段非法（INVALID_PARAMS）。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise model_unavailable("模型 manifest 不可读（未安装？）: %s" % path, field="model_id") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise model_unavailable("模型 manifest 非合法 JSON: %s" % path, field="model_id") from exc
    try:
        return validate_model_manifest(data)
    except TypeValidationError as exc:
        raise RecognitionError(exc.code, exc.message, field=exc.field,
                               retryable=exc.retryable) from exc
