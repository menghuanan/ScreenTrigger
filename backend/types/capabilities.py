# -*- coding: utf-8 -*-
"""协议伴生类型与共享校验工具（TASK-006 批 B1，设计 §3.3/§7.1）。

- Capability/CAPABILITY_IDS（§7.1 冻结七枚举）；
- 错误结构 {code,message,details,retryable}（06）+ 06 全量错误码枚举常量；
- ModelManifest/ProviderManifest（00 Provider 接口段；骨架期仅类型）；
- TypeValidationError：三端 validate() 纯函数共用的定位错误
  （code=06 错误码，details.field=逐字字段路径，如 "region.width"）；
- 纯校验共享工具（整数/字符串/键集/相对路径/hex64），无任何 IO。

零第三方依赖（设计 §4.3：Python 纯标准库 dataclasses）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 冻结枚举常量（schema/types.schema.json 同源）
# ---------------------------------------------------------------------------

CAPABILITY_IDS: Tuple[str, ...] = (
    "ocr",
    "lua_script",
    "template_match",
    "feature_match",
    "contour_match",
    "color_match",
    "input",
    "capture",
)
CAPABILITY_STATES: Tuple[str, ...] = ("available", "pending", "unavailable")

# 06 错误码注册表全量（40 码；顺序照 06 表）
ERROR_CODES: Tuple[str, ...] = (
    "INVALID_PARAMS",
    "INVALID_STATE",
    "BUSY",
    "CONFLICT",
    "IN_USE",
    "NOT_FOUND",
    "PROTOCOL_ERROR",
    "UNKNOWN_COMMAND",
    "DUPLICATE_EXPIRED",
    "ENGINE_UNAVAILABLE",
    "INTERNAL_ERROR",
    "DISPLAY_NOT_FOUND",
    "INVALID_REGION",
    "INVALID_IMAGE",
    "IMAGE_EXPIRED",
    "CAPTURE_FAILED",
    "TARGET_NOT_FOUND",
    "GROUP_NOT_FOUND",
    "MODEL_UNAVAILABLE",
    "LANGUAGE_UNSUPPORTED",
    "RECOGNITION_TIMEOUT",
    "RECOGNITION_FAILED",
    "SCRIPT_NOT_FOUND",
    "SCRIPT_SYNTAX_ERROR",
    "SCRIPT_RUNTIME_ERROR",
    "SCRIPT_TIMEOUT",
    "WAIT_TIMEOUT",
    "CALL_DEPTH_EXCEEDED",
    "SERIALIZATION_ERROR",
    "CAPABILITY_DENIED",
    "INPUT_FAILED",
    "REGION_LIMIT_VIOLATION",
    "STALE_FRAME",
    "WINDOW_NOT_FOUND",
    "WINDOW_UNAVAILABLE",
    "HOTKEY_CONFLICT",
    "STORAGE_ERROR",
    "DATA_CORRUPT",
    "SCHEMA_UNSUPPORTED",
    "RESOURCE_LIMIT",
)

CONDITION_TYPES: Tuple[str, ...] = ("template", "contour", "ocr", "color", "feature_match")

REGIONS_SPACES: Tuple[str, ...] = ("screen_physical", "window_client_physical", "image_pixel")

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


# ---------------------------------------------------------------------------
# 校验错误与共享纯函数工具（region/conditions/runtime 共用）
# ---------------------------------------------------------------------------

class TypeValidationError(ValueError):
    """类型纯校验错误：code=06 错误码，details.field=字段路径（三端逐字一致）。"""

    def __init__(self, code: str, field: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": {"field": self.field},
            "retryable": self.retryable,
        }


def fail(code: str, field: str, message: str) -> None:
    """抛出定位校验错误（约定式缩写）。"""
    raise TypeValidationError(code, field, message)


def check_keys(value: Dict[str, Any], allowed: Tuple[str, ...], prefix: str) -> None:
    """未知字段拒绝（02：未声明字段拒绝；metadata 例外由调用方声明）。"""
    for key in value:
        if key not in allowed:
            fail("INVALID_PARAMS", "%s.%s" % (prefix, key), "未声明字段: %s" % key)


def require_object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        fail("INVALID_PARAMS", field, "%s 必须为对象" % field)
    return value


def int_field(
    container: Dict[str, Any],
    name: str,
    field: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    code: str = "INVALID_PARAMS",
) -> int:
    """必填整数（bool 是 int 子类，显式拒绝）；范围越界按 code 报错。"""
    value = container.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        fail(code, field, "%s 必须为整数" % field)
    if minimum is not None and value < minimum:
        fail(code, field, "%s 不得小于 %d" % (field, minimum))
    if maximum is not None and value > maximum:
        fail(code, field, "%s 不得大于 %d" % (field, maximum))
    return value


def number_field(
    container: Dict[str, Any],
    name: str,
    field: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    exclusive_minimum: bool = False,
    code: str = "INVALID_PARAMS",
) -> float:
    """必填有限数（禁 NaN/Infinity：float('nan') 比较为 False，显式排除）。"""
    value = container.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(code, field, "%s 必须为数值" % field)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        fail(code, field, "%s 必须为有限数（禁 NaN/Infinity）" % field)
    if minimum is not None and (number < minimum or (exclusive_minimum and number == minimum)):
        fail(code, field, "%s 不得小于 %s" % (field, minimum))
    if maximum is not None and number > maximum:
        fail(code, field, "%s 不得大于 %s" % (field, maximum))
    return number


def str_field(
    container: Dict[str, Any],
    name: str,
    field: str,
    min_length: int = 1,
    max_length: Optional[int] = None,
    strip_first: bool = False,
) -> str:
    """必填字符串；strip_first=True 时按"去首尾空白后"计长（02 name 语义）。"""
    value = container.get(name)
    if not isinstance(value, str):
        fail("INVALID_PARAMS", field, "%s 必须为字符串" % field)
    text = value.strip() if strip_first else value
    if len(text) < min_length:
        fail("INVALID_PARAMS", field, "%s 长度不得少于 %d 字符" % (field, min_length))
    if max_length is not None and len(text) > max_length:
        fail("INVALID_PARAMS", field, "%s 长度不得超过 %d 字符" % (field, max_length))
    return text


def enum_field(container: Dict[str, Any], name: str, field: str, choices: Tuple[str, ...]) -> str:
    value = container.get(name)
    if not isinstance(value, str) or value not in choices:
        fail("INVALID_PARAMS", field, "%s 必须为 %s 之一" % (field, "/".join(choices)))
    return value


def bool_field(container: Dict[str, Any], name: str, field: str) -> bool:
    value = container.get(name)
    if not isinstance(value, bool):
        fail("INVALID_PARAMS", field, "%s 必须为布尔值" % field)
    return value


def hex64_field(container: Dict[str, Any], name: str, field: str) -> str:
    value = str_field(container, name, field)
    if _SHA256_RE.fullmatch(value) is None:
        fail("INVALID_PARAMS", field, "%s 必须为 64 位十六进制 sha256" % field)
    return value


def relative_path_field(container: Dict[str, Any], name: str, field: str) -> str:
    """数据根相对路径（02/07 §6）：禁绝对路径、盘符、..（路径组件判断，非 startswith）。"""
    value = str_field(container, name, field)
    if _DRIVE_RE.match(value) is not None:
        fail("INVALID_PARAMS", field, "%s 禁止盘符/绝对路径" % field)
    if value.startswith("/") or value.startswith("\\"):
        fail("INVALID_PARAMS", field, "%s 必须为数据根相对路径" % field)
    for component in value.replace("\\", "/").split("/"):
        if component == "..":
            fail("INVALID_PARAMS", field, "%s 禁止 .. 路径组件" % field)
    return value


def optional_field(container: Dict[str, Any], name: str) -> bool:
    """字段是否存在（"其他空间不出现"类规则用：出现即违例，含 null）。"""
    return name in container


# ---------------------------------------------------------------------------
# 伴生类型（dataclass frozen + validate 纯函数）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """能力声明 {id,state,reason}（TASK-005 冻结；reason 可 null）。"""

    id: str
    state: str
    reason: Optional[str] = None


def validate_capability(value: Dict[str, Any]) -> Capability:
    check_keys(value, ("id", "state", "reason"), "capability")
    cap_id = enum_field(value, "id", "capability.id", CAPABILITY_IDS)
    state = enum_field(value, "state", "capability.state", CAPABILITY_STATES)
    reason = value.get("reason")
    if reason is not None and not isinstance(reason, str):
        fail("INVALID_PARAMS", "capability.reason", "capability.reason 必须为字符串或 null")
    return Capability(id=cap_id, state=state, reason=reason)


@dataclass(frozen=True)
class IpcError:
    """错误结构 {code,message,details,retryable}（06 四字段）。"""

    code: str
    message: str
    details: Dict[str, Any]
    retryable: bool


def validate_error_structure(value: Dict[str, Any]) -> IpcError:
    check_keys(value, ("code", "message", "details", "retryable"), "error")
    code = enum_field(value, "code", "error.code", ERROR_CODES)
    message = str_field(value, "message", "error.message")
    details = value.get("details")
    if not isinstance(details, dict):
        fail("INVALID_PARAMS", "error.details", "error.details 必须为对象")
    retryable = bool_field(value, "retryable", "error.retryable")
    return IpcError(code=code, message=message, details=details, retryable=retryable)


@dataclass(frozen=True)
class ProviderManifest:
    """00 Provider 接口段：api_version 首版为 1，未知 major 拒绝初始化。"""

    provider_id: str
    api_version: int
    provider_version: str
    supported_condition_types: Tuple[str, ...]
    model_ids: Tuple[str, ...]
    supported_languages: Tuple[str, ...]
    thread_safe: bool
    offline: bool


def _validate_provider_manifest_fields(
    value: Dict[str, Any], prefix: str
) -> Dict[str, Any]:
    provider_id = str_field(value, "provider_id", prefix + ".provider_id")
    api_version = int_field(value, "api_version", prefix + ".api_version", minimum=1)
    provider_version = str_field(value, "provider_version", prefix + ".provider_version")
    condition_types = value.get("supported_condition_types")
    if not isinstance(condition_types, list):
        fail("INVALID_PARAMS", prefix + ".supported_condition_types",
             "supported_condition_types 必须为数组")
    for item in condition_types:
        if item not in CONDITION_TYPES:
            fail("INVALID_PARAMS", prefix + ".supported_condition_types",
                 "未知条件类型: %r" % (item,))
    model_ids = value.get("model_ids")
    if not isinstance(model_ids, list) or not all(
        isinstance(item, str) and item for item in model_ids
    ):
        fail("INVALID_PARAMS", prefix + ".model_ids", "model_ids 必须为非空字符串数组")
    languages = value.get("supported_languages")
    if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
        fail("INVALID_PARAMS", prefix + ".supported_languages",
             "supported_languages 必须为字符串数组")
    thread_safe = bool_field(value, "thread_safe", prefix + ".thread_safe")
    offline = bool_field(value, "offline", prefix + ".offline")
    return {
        "provider_id": provider_id,
        "api_version": api_version,
        "provider_version": provider_version,
        "supported_condition_types": tuple(condition_types),
        "model_ids": tuple(model_ids),
        "supported_languages": tuple(languages),
        "thread_safe": thread_safe,
        "offline": offline,
    }


def validate_provider_manifest(value: Dict[str, Any]) -> ProviderManifest:
    check_keys(
        value,
        (
            "provider_id", "api_version", "provider_version", "supported_condition_types",
            "model_ids", "supported_languages", "thread_safe", "offline",
        ),
        "provider_manifest",
    )
    return ProviderManifest(**_validate_provider_manifest_fields(value, "provider_manifest"))


@dataclass(frozen=True)
class ModelManifest:
    """00：模型 manifest = ProviderManifest 全字段 + 许可证/来源/sha256/字符集覆盖。"""

    provider_id: str
    api_version: int
    provider_version: str
    supported_condition_types: Tuple[str, ...]
    model_ids: Tuple[str, ...]
    supported_languages: Tuple[str, ...]
    thread_safe: bool
    offline: bool
    license: str
    source: str
    sha256: str
    supported_charsets: Tuple[str, ...]


def validate_model_manifest(value: Dict[str, Any]) -> ModelManifest:
    check_keys(
        value,
        (
            "provider_id", "api_version", "provider_version", "supported_condition_types",
            "model_ids", "supported_languages", "thread_safe", "offline",
            "license", "source", "sha256", "supported_charsets",
        ),
        "model_manifest",
    )
    base = _validate_provider_manifest_fields(value, "model_manifest")
    license_name = str_field(value, "license", "model_manifest.license")
    source = str_field(value, "source", "model_manifest.source")
    sha256 = hex64_field(value, "sha256", "model_manifest.sha256")
    charsets = value.get("supported_charsets")
    if not isinstance(charsets, list) or not all(isinstance(item, str) for item in charsets):
        fail("INVALID_PARAMS", "model_manifest.supported_charsets",
             "supported_charsets 必须为字符串数组")
    return ModelManifest(supported_charsets=tuple(charsets), license=license_name,
                         source=source, sha256=sha256, **base)
