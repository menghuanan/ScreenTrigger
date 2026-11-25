# -*- coding: utf-8 -*-
"""脚本领域层（TASK-012）：纯校验 + 不可变元数据文档变换（无 IO / 无落盘 / 无事件）。

职责边界（设计 §3/§4）：

- **只**做纯函数：不落盘、不 commit、不读文件、不发事件、**不 import lupa**；
- 依赖仅限 ``types.conditions``（权威 Script/参数校验器）、``storage.errors``（06 四字段）
  与 ``storage.generation`` 常量，以及标准库；
- 所有输入按 Mapping/Sequence 处理，输出为全新 dict/tuple，绝不原地修改共享快照。

覆盖设计 §5/§6/§7 的领域规则：

- 源码：``str`` + 严格 UTF-8（含孤立代理对拒绝）+ 编码后**字节** ≤ 1MiB（Q8）；
- 名称：去首尾空白后 1–100 Unicode 码点；
- 参数：复用 ``types.conditions._validate_script_parameter`` 五字段规则 + **参数名唯一性**
  （Q9）+ ``default`` 合法 JsonValue 且一级类型与声明 ``type`` 一致（Q9）；
- ``Script`` 元数据 dict 构造（create revision=1 / update old+1，时间戳由调用方注入）；
- ``source_path`` 构造 ``scripts/<id>/<source_sha256>.lua``（复用 ``_SCRIPT_SOURCE_RE``）；
- 元数据文档（``{"schema_version":2,"scripts":[...]}``）读写（纯 dict 变换）；
- script 定位 / revision 判定（storage 不认 script，见 §8.1）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..storage.errors import StorageError, conflict, invalid_params, resource_limit
from ..storage.generation import SCHEMA_VERSION
from ..types.capabilities import TypeValidationError
from ..types.conditions import _SCRIPT_SOURCE_RE, _validate_script_parameter

# ---------------------------------------------------------------------------
# 常量与哨兵
# ---------------------------------------------------------------------------

#: 「键缺省」哨兵：区分 ``save_script`` 的「同时省=新建」与「同时给=更新」（§7.1）。
UNSET: Any = object()

#: 源码预算（06/02：UTF-8 编码后**字节**数 ≤ 1 MiB）。
SOURCE_MAX_BYTES: int = 1048576

#: 名称长度（02：去首尾空白后 1–100 Unicode 码点）。
NAME_MIN: int = 1
NAME_MAX: int = 100

#: 源码内容寻址目录（02 磁盘布局：``scripts/<id>/<sha>.lua``）。
SCRIPT_DIR: str = "scripts"

#: Script 元数据键集（02 逐字）。
SCRIPT_KEYS: Tuple[str, ...] = (
    "id",
    "revision",
    "name",
    "source_path",
    "source_sha256",
    "parameters",
    "created_at",
    "updated_at",
)

#: 参数一级类型枚举（02）。
PARAM_TYPES: Tuple[str, ...] = ("string", "number", "boolean", "object", "array")

#: ``default`` JsonValue 嵌套深度上限（Q9：防深度炸弹）。
MAX_JSON_DEPTH: int = 32


# ---------------------------------------------------------------------------
# 元数据文档读写（纯 dict 变换）
# ---------------------------------------------------------------------------

def scripts_from_snapshot(snapshot: Any) -> List[Mapping[str, Any]]:
    """从快照 ``script_metadata`` 解析脚本列表；缺失/非列表 → ``[]``（缺省最小信封）。"""
    metadata = getattr(snapshot, "script_metadata", None)
    if not isinstance(metadata, Mapping):
        return []
    scripts = metadata.get("scripts")
    if not isinstance(scripts, list):
        return []
    return [item for item in scripts if isinstance(item, Mapping)]


def script_metadata_document(scripts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """构造可落盘的 ``script_metadata.json`` 文档（02：``{schema_version,scripts}``）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "scripts": [dict(item) for item in scripts],
    }


def find_script(
    scripts: Sequence[Mapping[str, Any]], script_id: str
) -> Optional[Mapping[str, Any]]:
    """按 id 定位脚本；不存在返回 None。"""
    for script in scripts:
        if script.get("id") == script_id:
            return script
    return None


def ensure_script_found(
    script: Optional[Mapping[str, Any]], script_id: str
) -> Mapping[str, Any]:
    """脚本不存在 → ``SCRIPT_NOT_FOUND`` field=script_id。"""
    if script is None:
        raise StorageError(
            "SCRIPT_NOT_FOUND", "脚本不存在: %s" % script_id, field="script_id"
        )
    return script


def ensure_script_revision(script: Mapping[str, Any], expected_revision: int) -> None:
    """脚本 revision 不符 → ``CONFLICT`` field=expected_revision（§8.1，service 自查）。"""
    if int(script["revision"]) != int(expected_revision):
        raise conflict(
            "脚本 revision 不符（期望 %s 实际 %s）" % (expected_revision, script["revision"]),
            field="expected_revision",
        )


def sort_scripts(scripts: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """列表排序：``created_at`` 升序，tie-break ``id`` 升序（Q6，跨存储确定性）。"""
    return sorted(scripts, key=lambda s: (str(s.get("created_at", "")), str(s.get("id", ""))))


def upsert_script(
    scripts: Sequence[Mapping[str, Any]], script: Mapping[str, Any]
) -> Tuple[Mapping[str, Any], ...]:
    """按 id 覆盖或追加脚本，返回全新元组（不原地修改）。"""
    result: List[Mapping[str, Any]] = []
    replaced = False
    for item in scripts:
        if item.get("id") == script["id"]:
            result.append(dict(script))
            replaced = True
        else:
            result.append(dict(item))
    if not replaced:
        result.append(dict(script))
    return tuple(result)


def remove_script(
    scripts: Sequence[Mapping[str, Any]], script_id: str
) -> Tuple[Mapping[str, Any], ...]:
    """删除指定 id 的脚本，返回全新元组。"""
    return tuple(dict(item) for item in scripts if item.get("id") != script_id)


# ---------------------------------------------------------------------------
# 名称 / 源码 / 参数校验
# ---------------------------------------------------------------------------

def normalize_name(name: Any) -> str:
    """名称规范化：去首尾空白后 1–100 Unicode 码点，否则 ``INVALID_PARAMS`` field=name。"""
    if not isinstance(name, str):
        raise invalid_params("name", "name 必须为字符串")
    normalized = name.strip()
    if not NAME_MIN <= len(normalized) <= NAME_MAX:
        raise invalid_params("name", "name 去首尾空白后须为 1–100 字符")
    return normalized


def validate_source(source: Any) -> bytes:
    """源码校验：``str`` + 严格 UTF-8 + 编码字节 ≤ 1MiB；返回编码后字节（Q8）。

    Raises:
        StorageError: INVALID_PARAMS field=source（非 str / 含孤立代理对）；
            RESOURCE_LIMIT field=source（> 1MiB）。
    """
    if not isinstance(source, str):
        raise invalid_params("source", "source 必须为字符串")
    try:
        raw = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        # 含孤立代理对（如 JSON "\ud800"）→ 拒绝（02/06 UTF-8 严格）
        raise invalid_params("source", "source 必须为合法 UTF-8 文本: %s" % exc) from exc
    if len(raw) > SOURCE_MAX_BYTES:
        raise resource_limit(
            "source 超过 %d 字节上限（实际 %d）" % (SOURCE_MAX_BYTES, len(raw)),
            details={"field": "source"},
        )
    return raw


def _is_json_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_json_value(value: Any, depth: int) -> None:
    """``default`` 必须是合法 JsonValue：无 NaN/Inf、键为字符串、深度受限（Q9）。"""
    if depth > MAX_JSON_DEPTH:
        raise invalid_params("parameters", "default 嵌套层级超过 %d" % MAX_JSON_DEPTH)
    if value is None or isinstance(value, (str, bool)):
        return
    if _is_json_number(value):
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            raise invalid_params("parameters", "default 含非法数值（NaN/Infinity）")
        return
    if isinstance(value, list):
        for item in value:
            _check_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise invalid_params("parameters", "default 对象键必须为字符串")
            _check_json_value(item, depth + 1)
        return
    raise invalid_params("parameters", "default 必须为合法 JsonValue")


def parameter_type_matches(default: Any, type_name: str) -> bool:
    """``default`` 一级类型是否与声明 ``type`` 一致（Q9；``default=null`` 由调用方跳过）。"""
    if type_name == "string":
        return isinstance(default, str)
    if type_name == "number":
        return _is_json_number(default)
    if type_name == "boolean":
        return isinstance(default, bool)
    if type_name == "object":
        return isinstance(default, dict)
    if type_name == "array":
        return isinstance(default, list)
    return False


def validate_parameters(parameters: Any) -> Tuple[Dict[str, Any], ...]:
    """参数校验：数组 + 五字段（复用 ``_validate_script_parameter``）+ 名称唯一性（Q9）。

    Raises:
        StorageError: INVALID_PARAMS field=``parameters``（或 ``parameters.<k>``）。
    """
    if not isinstance(parameters, list):
        raise invalid_params("parameters", "parameters 必须为数组")
    seen: set = set()
    result: List[Dict[str, Any]] = []
    for item in parameters:
        try:
            param = _validate_script_parameter(item)
        except TypeValidationError as exc:
            raise StorageError(
                exc.code, exc.message, details={"field": exc.field}, retryable=exc.retryable
            ) from exc
        if param.name in seen:
            raise invalid_params("parameters", "参数名重复: %s" % param.name)
        seen.add(param.name)
        default = param.default
        if default is not None:
            _check_json_value(default, 0)
            if not parameter_type_matches(default, param.type):
                raise invalid_params(
                    "parameters",
                    "参数 %s 的 default 类型与声明的 type(%s) 不一致" % (param.name, param.type),
                )
        result.append(
            {
                "name": param.name,
                "type": param.type,
                "required": param.required,
                "default": default,
                "description": param.description,
            }
        )
    return tuple(result)


def validate_script_input(
    name: Any, source: Any, parameters: Any
) -> Tuple[str, bytes, Tuple[Dict[str, Any], ...]]:
    """聚合校验（顺序：name → source → parameters），返回 ``(name, source_bytes, params)``。"""
    return normalize_name(name), validate_source(source), validate_parameters(parameters)


# ---------------------------------------------------------------------------
# 源码路径 / Script 构造
# ---------------------------------------------------------------------------

def source_relative_path(script_id: Any, source_sha256: str) -> str:
    """构造 ``scripts/<id>/<source_sha256>.lua``；``<id>`` 组件合法且形态匹配（02/07）。

    Raises:
        StorageError: INVALID_PARAMS field=source_path。
    """
    if not isinstance(script_id, str) or not script_id:
        raise invalid_params("source_path", "script id 必须为非空字符串")
    if script_id in (".", "..") or "/" in script_id or "\\" in script_id:
        raise invalid_params("source_path", "script id 不得含路径分隔符或 . / ..")
    relative = "%s/%s/%s.lua" % (SCRIPT_DIR, script_id, source_sha256)
    if _SCRIPT_SOURCE_RE.fullmatch(relative) is None:
        raise invalid_params(
            "source_path", "source_path 必须为 scripts/<id>/<hash>.lua 形态"
        )
    return relative


def build_script_dict(
    script_id: str,
    revision: int,
    name: str,
    source_path: str,
    source_sha256: str,
    parameters: Sequence[Mapping[str, Any]],
    created_at: str,
    updated_at: str,
) -> Dict[str, Any]:
    """构造 02 形态 Script dict（键名/键序逐字；parameters 深拷贝为 list）。"""
    return {
        "id": script_id,
        "revision": int(revision),
        "name": name,
        "source_path": source_path,
        "source_sha256": source_sha256,
        "parameters": [dict(item) for item in parameters],
        "created_at": created_at,
        "updated_at": updated_at,
    }


def is_noop_save(
    script: Mapping[str, Any],
    name: str,
    parameters: Sequence[Mapping[str, Any]],
    source_sha256: str,
) -> bool:
    """同 ``(name, parameters, source_sha256)`` 重复保存判定（Q3：no-op）。"""
    if str(script.get("name")) != name:
        return False
    if str(script.get("source_sha256")) != source_sha256:
        return False
    existing = script.get("parameters")
    if not isinstance(existing, list):
        return False
    normalized_existing = [
        {
            "name": item.get("name"),
            "type": item.get("type"),
            "required": item.get("required"),
            "default": item.get("default"),
            "description": item.get("description"),
        }
        for item in existing
        if isinstance(item, Mapping)
    ]
    normalized_new = [
        {
            "name": item.get("name"),
            "type": item.get("type"),
            "required": item.get("required"),
            "default": item.get("default"),
            "description": item.get("description"),
        }
        for item in parameters
    ]
    return normalized_existing == normalized_new
