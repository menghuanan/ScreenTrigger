# -*- coding: utf-8 -*-
"""目标/条件树/分组/脚本类型（TASK-006 批 B1，设计 §3.2/§6.2；schema 同源）。

02 权威规则：
- 条件树：分支 {op: all|any, children:[...]}/叶 {id, type, params, roi?}；
  叶 1–10、嵌套最多 4 层（分支层数，叶不计）、叶 ID 目标内唯一；
  anchor_condition_id 必须指向存在叶（整体语义=anchor 命中 AND 条件树为真）；
- 条件叶 params 四类逐字段（template/contour/ocr/color），错误定位固定前缀
  "conditions.params.*"（契约用例逐字口径，与叶在树中位置无关）；
- color_space 只接受 rgb|hsv，`hsc` 生成即 INVALID_PARAMS（02 明文）；
- HSV h∈[0,360)（整数域 0..359）s/v∈[0,100]；容差域 h 0–180（圆周最短距离）；
- canny_low < canny_high（相等即违例）0–255；
- Target：name 去首尾空白后 1–100 Unicode 字符；sample_no/revision≥1；
  metadata ≤16KiB JSON 对象（唯一允许未知字段的例外）；
- Group：depth 0..2；parent 顶层 null、否则 depth≥1（跨实体同层同名唯一
  归 Repository，纯函数只做 validate_group_fields()）；
- Script：source_path 形态 scripts/<id>/<hash>.lua 且路径组件判断拒绝 ..；
  参数 required=true 时 default 必须 null。

全部 validate_* 为纯函数（无 IO），错误统一 TypeValidationError。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from .capabilities import (
    bool_field,
    check_keys,
    enum_field,
    fail,
    int_field,
    number_field,
    relative_path_field,
    require_object,
    str_field,
    hex64_field,
)
from .region import (
    ImageAsset,
    validate_image_asset,
    validate_local_rect,
    validate_rfc3339_utc,
)

_SCRIPT_SOURCE_RE = re.compile(r"^scripts/[^/]+/[^/]+\.lua$")
_METADATA_MAX_BYTES = 16 * 1024  # 02：metadata 最多 16KiB
_MAX_LEAVES = 10  # 02：叶 1–10
_MAX_BRANCH_DEPTH = 4  # 02：嵌套最多 4 层（分支节点计数）

_SCRIPT_PARAM_TYPES = ("string", "number", "boolean", "object", "array")
_MATCH_MODES = ("exact", "contains", "fuzzy")


# ---------------------------------------------------------------------------
# 条件叶 params（错误定位固定前缀 conditions.params.*）
# ---------------------------------------------------------------------------

def _validate_template_params(params: Dict[str, Any]) -> None:
    check_keys(params, ("threshold", "allow_scale", "scale_range", "step"),
               "conditions.params")
    number_field(params, "threshold", "conditions.params.threshold", 0.0, 1.0)
    if "allow_scale" in params:
        bool_field(params, "allow_scale", "conditions.params.allow_scale")
    if "scale_range" in params:
        scale_range = params.get("scale_range")
        if (not isinstance(scale_range, list) or len(scale_range) != 2):
            fail("INVALID_PARAMS", "conditions.params.scale_range",
                 "conditions.params.scale_range 必须为二元数组")
        for index, item in enumerate(scale_range):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                fail("INVALID_PARAMS", "conditions.params.scale_range",
                     "scale_range 元素必须为数值")
            if item < 0 or item > 1:
                fail("INVALID_PARAMS", "conditions.params.scale_range",
                     "scale_range 元素须在 0..1")
        if scale_range[0] > scale_range[1]:
            fail("INVALID_PARAMS", "conditions.params.scale_range",
                 "scale_range 下界不得大于上界")
    if "step" in params:
        number_field(params, "step", "conditions.params.step", exclusive_minimum=True)


def _validate_contour_params(params: Dict[str, Any]) -> None:
    check_keys(params, ("threshold", "min_area", "canny_low", "canny_high"),
               "conditions.params")
    number_field(params, "threshold", "conditions.params.threshold", 0.0, 1.0)
    if "min_area" in params:
        int_field(params, "min_area", "conditions.params.min_area", minimum=1)
    canny_low = int_field(params, "canny_low", "conditions.params.canny_low",
                          minimum=0, maximum=255)
    canny_high = int_field(params, "canny_high", "conditions.params.canny_high",
                           minimum=0, maximum=255)
    # 02：canny_low < canny_high（相等即违例），定位 canny_high
    if canny_low >= canny_high:
        fail("INVALID_PARAMS", "conditions.params.canny_high",
             "canny_low 必须小于 canny_high")


def _validate_ocr_params(params: Dict[str, Any]) -> None:
    check_keys(
        params,
        ("text", "model_id", "match_mode", "min_confidence", "case_sensitive",
         "fuzzy_threshold"),
        "conditions.params",
    )
    str_field(params, "text", "conditions.params.text")
    str_field(params, "model_id", "conditions.params.model_id")
    if "match_mode" in params:
        enum_field(params, "match_mode", "conditions.params.match_mode", _MATCH_MODES)
    if "min_confidence" in params:
        number_field(params, "min_confidence", "conditions.params.min_confidence",
                     0.0, 1.0)
    if "case_sensitive" in params:
        bool_field(params, "case_sensitive", "conditions.params.case_sensitive")
    if "fuzzy_threshold" in params:
        number_field(params, "fuzzy_threshold", "conditions.params.fuzzy_threshold",
                     0.0, 1.0)


def _check_rgb_color(color: Dict[str, Any], field: str) -> None:
    for channel in ("r", "g", "b"):
        int_field(color, channel, "%s.%s" % (field, channel), 0, 255)


def _check_hsv_color(color: Dict[str, Any], field: str,
                     h_max: int) -> None:
    # 02：HSV h∈[0,360)（整数域 0..359）；h_max=180 用于容差（圆周最短距离）
    int_field(color, "h", field + ".h", 0, h_max)
    for channel in ("s", "v"):
        int_field(color, channel, "%s.%s" % (field, channel), 0, 100)


def _validate_color_params(params: Dict[str, Any]) -> None:
    check_keys(
        params,
        ("color_space", "color", "tolerance", "min_pixel_count", "min_percentage"),
        "conditions.params",
    )
    color_space = enum_field(params, "color_space", "conditions.params.color_space",
                             ("rgb", "hsv"))
    color = require_object(params.get("color"), "conditions.params.color")
    if color_space == "rgb":
        check_keys(color, ("r", "g", "b"), "conditions.params.color")
        _check_rgb_color(color, "conditions.params.color")
        if "tolerance" in params:
            tolerance = require_object(params.get("tolerance"),
                                       "conditions.params.tolerance")
            check_keys(tolerance, ("r", "g", "b"), "conditions.params.tolerance")
            _check_rgb_color(tolerance, "conditions.params.tolerance")
    else:  # hsv
        check_keys(color, ("h", "s", "v"), "conditions.params.color")
        _check_hsv_color(color, "conditions.params.color", 359)
        if "tolerance" in params:
            tolerance = require_object(params.get("tolerance"),
                                       "conditions.params.tolerance")
            check_keys(tolerance, ("h", "s", "v"), "conditions.params.tolerance")
            _check_hsv_color(tolerance, "conditions.params.tolerance", 180)
    if "min_pixel_count" in params:
        int_field(params, "min_pixel_count", "conditions.params.min_pixel_count",
                  minimum=1)
    if "min_percentage" in params:
        number_field(params, "min_percentage", "conditions.params.min_percentage",
                     0.0, 100.0)


_PARAM_VALIDATORS = {
    "template": _validate_template_params,
    "contour": _validate_contour_params,
    "ocr": _validate_ocr_params,
    "color": _validate_color_params,
}


# ---------------------------------------------------------------------------
# 条件树遍历（叶数/深度/唯一性 → 定位 "conditions"）
# ---------------------------------------------------------------------------

def _validate_condition_node(node: Any, branch_depth: int, leaf_ids: List[str],
                             allow_unknown_ids: bool) -> None:
    """递归校验条件树；branch_depth 为当前分支层数（叶不计层）。"""
    data = require_object(node, "conditions")
    if "op" in data or "children" in data:
        # 分支节点
        check_keys(data, ("op", "children"), "conditions")
        enum_field(data, "op", "conditions.op", ("all", "any"))
        children = data.get("children")
        if not isinstance(children, list) or len(children) < 1:
            fail("INVALID_PARAMS", "conditions.children",
                 "conditions.children 必须为非空数组")
        depth = branch_depth + 1
        if depth > _MAX_BRANCH_DEPTH:
            # 02：嵌套最多 4 层（分支节点计数），第 5 层拒绝
            fail("INVALID_PARAMS", "conditions", "条件树嵌套不得超过 4 层")
        for child in children:
            _validate_condition_node(child, depth, leaf_ids, allow_unknown_ids)
        return
    # 叶节点
    check_keys(data, ("id", "type", "params", "roi"), "conditions")
    leaf_id = str_field(data, "id", "conditions.id")
    leaf_type = enum_field(data, "type", "conditions.type",
                           ("template", "contour", "ocr", "color"))
    params = require_object(data.get("params"), "conditions.params")
    _PARAM_VALIDATORS[leaf_type](params)
    if "roi" in data:
        validate_local_rect(data.get("roi"), prefix="conditions.roi")
    if not allow_unknown_ids:
        # 叶 ID 目标内唯一（02）
        if leaf_id in leaf_ids:
            fail("INVALID_PARAMS", "conditions", "条件叶 ID 重复: %s" % leaf_id)
    leaf_ids.append(leaf_id)


def _validate_condition_tree(tree: Any) -> List[str]:
    """校验整棵树并返回叶 ID 列表（供 anchor 存在性检查）。"""
    leaf_ids: List[str] = []
    _validate_condition_node(tree, 0, leaf_ids, allow_unknown_ids=False)
    if not 1 <= len(leaf_ids) <= _MAX_LEAVES:
        # 02：叶 1–10
        fail("INVALID_PARAMS", "conditions",
             "条件树叶数必须为 1–%d 个，实际 %d 个" % (_MAX_LEAVES, len(leaf_ids)))
    return leaf_ids


# ---------------------------------------------------------------------------
# Target / Group / Script
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """02：目标库实体（纯类型；Repository 读写属 TASK-009/010）。"""

    id: str
    sample_no: int
    revision: int
    name: str
    group_id: Optional[str]
    image: ImageAsset
    thumbnail_path: Optional[str]
    anchor_condition_id: str
    conditions: Dict[str, Any]
    created_at: str
    updated_at: str
    metadata: Dict[str, Any]


def _validate_metadata_size(metadata: Any, field: str = "metadata") -> None:
    if not isinstance(metadata, dict):
        fail("INVALID_PARAMS", field, "%s 必须为对象" % field)
    size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
    if size > _METADATA_MAX_BYTES:
        fail("INVALID_PARAMS", field,
             "%s 序列化后不得超过 16KiB，实际 %d 字节" % (field, size))


def _validate_optional_str_or_null(container: Dict[str, Any], name: str,
                                   field: str) -> Optional[str]:
    value = container.get(name)
    if value is not None and not isinstance(value, str):
        fail("INVALID_PARAMS", field, "%s 必须为字符串或 null" % field)
    return value


def validate_target(value: Any) -> Target:
    data = require_object(value, "target")
    check_keys(
        data,
        ("id", "sample_no", "revision", "name", "group_id", "image",
         "thumbnail_path", "anchor_condition_id", "conditions", "created_at",
         "updated_at", "metadata"),
        "target",
    )
    # name：去首尾空白后 1–100 字符（02），存规范化后的值
    raw_name = data.get("name")
    if not isinstance(raw_name, str):
        fail("INVALID_PARAMS", "name", "name 必须为字符串")
    name = raw_name.strip()
    if not 1 <= len(name) <= 100:
        fail("INVALID_PARAMS", "name", "name 去首尾空白后须为 1–100 字符")

    anchor_condition_id = str_field(data, "anchor_condition_id",
                                    "anchor_condition_id")
    leaf_ids = _validate_condition_tree(data.get("conditions"))
    if anchor_condition_id not in leaf_ids:
        # 02：anchor 必须指向存在叶
        fail("INVALID_PARAMS", "anchor_condition_id",
             "anchor_condition_id 必须指向存在的条件叶")
    metadata = data.get("metadata")
    _validate_metadata_size(metadata)
    return Target(
        id=str_field(data, "id", "id"),
        sample_no=int_field(data, "sample_no", "sample_no", minimum=1),
        revision=int_field(data, "revision", "revision", minimum=1),
        name=name,
        group_id=_validate_optional_str_or_null(data, "group_id", "group_id"),
        image=validate_image_asset(data.get("image")),
        thumbnail_path=_validate_optional_str_or_null(data, "thumbnail_path",
                                                      "thumbnail_path"),
        anchor_condition_id=anchor_condition_id,
        conditions=data.get("conditions"),
        created_at=validate_rfc3339_utc(data.get("created_at"), "created_at"),
        updated_at=validate_rfc3339_utc(data.get("updated_at"), "updated_at"),
        metadata=metadata,
    )


@dataclass(frozen=True)
class Group:
    """02：分组（最多 3 层 depth 0..2）；同层同名唯一归 Repository。"""

    id: str
    revision: int
    name: str
    parent_group_id: Optional[str]
    depth: int
    created_at: str
    updated_at: str


def validate_group_fields(value: Any) -> Group:
    """纯字段校验；跨实体同层同名唯一（NFC+大小写折叠）需上下文，归 Repository。"""
    data = require_object(value, "group")
    check_keys(
        data,
        ("id", "revision", "name", "parent_group_id", "depth", "created_at",
         "updated_at"),
        "group",
    )
    raw_name = data.get("name")
    if not isinstance(raw_name, str):
        fail("INVALID_PARAMS", "group.name", "group.name 必须为字符串")
    name = raw_name.strip()
    if not 1 <= len(name) <= 100:
        fail("INVALID_PARAMS", "group.name", "group.name 去首尾空白后须为 1–100 字符")
    parent_group_id = _validate_optional_str_or_null(data, "parent_group_id",
                                                     "group.parent_group_id")
    depth = int_field(data, "depth", "group.depth", minimum=0, maximum=2)
    # 02：parent 顶层 null（depth=0）、否则 depth≥1（=父 depth+1 由 Repository 保证）
    if parent_group_id is None and depth != 0:
        fail("INVALID_PARAMS", "group.depth", "顶层分组 depth 必须为 0")
    if parent_group_id is not None and depth < 1:
        fail("INVALID_PARAMS", "group.depth", "子分组 depth 必须不小于 1")
    return Group(
        id=str_field(data, "id", "group.id"),
        revision=int_field(data, "revision", "group.revision", minimum=1),
        name=name,
        parent_group_id=parent_group_id,
        depth=depth,
        created_at=validate_rfc3339_utc(data.get("created_at"), "group.created_at"),
        updated_at=validate_rfc3339_utc(data.get("updated_at"), "group.updated_at"),
    )


@dataclass(frozen=True)
class ScriptParameter:
    """02：只声明一级类型，嵌套 JsonValue；required=true 时 default 必须 null。"""

    name: str
    type: str
    required: bool
    default: Any
    description: str


@dataclass(frozen=True)
class Script:
    """02：source_path 形态 scripts/<id>/<hash>.lua；source UTF-8 ≤1MiB 属 load 时。"""

    id: str
    revision: int
    name: str
    source_path: str
    source_sha256: str
    parameters: Tuple[ScriptParameter, ...]
    created_at: str
    updated_at: str


def _validate_script_parameter(item: Any) -> ScriptParameter:
    data = require_object(item, "parameters")
    check_keys(data, ("name", "type", "required", "default", "description"),
               "parameters")
    required = bool_field(data, "required", "parameters.required")
    if required and data.get("default") is not None:
        # 02：required=true 时 default 必须 null
        fail("INVALID_PARAMS", "parameters",
             "required=true 的参数 default 必须为 null")
    return ScriptParameter(
        name=str_field(data, "name", "parameters.name"),
        type=enum_field(data, "type", "parameters.type", _SCRIPT_PARAM_TYPES),
        required=required,
        default=data.get("default"),
        description=data.get("description")
        if isinstance(data.get("description"), str) else "",
    )


def validate_script(value: Any) -> Script:
    data = require_object(value, "script")
    check_keys(
        data,
        ("id", "revision", "name", "source_path", "source_sha256", "parameters",
         "created_at", "updated_at"),
        "script",
    )
    raw_name = data.get("name")
    if not isinstance(raw_name, str):
        fail("INVALID_PARAMS", "name", "name 必须为字符串")
    name = raw_name.strip()
    if not 1 <= len(name) <= 100:
        fail("INVALID_PARAMS", "name", "name 去首尾空白后须为 1–100 字符")
    # 路径组件判断（07 §6）：先按相对路径拒 .. /盘符/绝对，再对形态 scripts/<id>/<hash>.lua
    source_path = relative_path_field(data, "source_path", "source_path")
    if _SCRIPT_SOURCE_RE.fullmatch(source_path) is None:
        fail("INVALID_PARAMS", "source_path",
             "source_path 必须为 scripts/<id>/<hash>.lua 形态")
    parameters = data.get("parameters")
    if not isinstance(parameters, list):
        fail("INVALID_PARAMS", "parameters", "parameters 必须为数组")
    return Script(
        id=str_field(data, "id", "id"),
        revision=int_field(data, "revision", "revision", minimum=1),
        name=name,
        source_path=source_path,
        source_sha256=hex64_field(data, "source_sha256", "source_sha256"),
        parameters=tuple(_validate_script_parameter(item) for item in parameters),
        created_at=validate_rfc3339_utc(data.get("created_at"), "created_at"),
        updated_at=validate_rfc3339_utc(data.get("updated_at"), "updated_at"),
    )
