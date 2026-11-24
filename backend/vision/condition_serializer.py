# -*- coding: utf-8 -*-
"""条件树解析 / 校验 / 规范化（TASK-016 T16-1；**纯函数，仅标准库 + backend.types**）。

把 02 权威形态的条件树 ``{op:all|any, children:[...]}`` / 叶 ``{id,type,params,roi?}``
解析为**有序 spec**（DFS 前序，顺序确定），供 ``condition_evaluator`` 逐候选求值：

- **复用** ``backend/types/conditions.py`` 的权威校验（叶 params 四类 + ``roi`` 经
  ``validate_local_rect``）：叶子/深度/唯一性/叶数 1–10 由 ``_validate_condition_tree``
  保证；本模块**不重造**这些规则，只在其上追加「有序 spec + anchor 解析 + ROI 夹取」。
- **无 NOT**：分支仅 ``all``/``any``（``_validate_condition_tree`` 的 op 枚举拒绝 ``not``）。
- ``anchor_condition_id`` 必须指向存在的叶（02）。

语义（02:78）：
- ``anchor`` 叶在**整帧**上产候选且必须成功（其 ``roi`` **被忽略**并记一条诊断）；
- 其余叶在**候选 bbox 内**评估；整体 = ``anchor 命中 AND 条件树为真``。

ROI（02:22）：叶 ``roi`` 为 ``LocalRect``，**相对候选 bbox**（宽高 ≥1），非 Region；
``leaf_roi_in_frame`` 把其夹取到帧内（越界夹取；完全落帧外 → ``None`` → 该叶判 false）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from ..types.capabilities import TypeValidationError
from ..types.conditions import (
    _validate_color_params,
    _validate_condition_tree,
    _validate_contour_params,
    _validate_ocr_params,
    _validate_template_params,
    validate_target,
)
from ..types.region import LocalRect, Rect, validate_local_rect

#: 叶类型枚举（02；与 types.conditions 逐字）。
LEAF_TYPES: Tuple[str, ...] = ("template", "contour", "ocr", "color")

#: 叶 params 校验器（权威复用，不重造）。
_PARAM_VALIDATORS = {
    "template": _validate_template_params,
    "contour": _validate_contour_params,
    "ocr": _validate_ocr_params,
    "color": _validate_color_params,
}

# ---------------------------------------------------------------------------
# 草稿（TargetDraft）校验（03 逐字键集；复用 types.conditions.validate_target 的占位手法）
# ---------------------------------------------------------------------------

#: 草稿键集（03：`{name,group_id?,image_id,anchor_condition_id,conditions,storage_mode?}`）。
DRAFT_KEYS: Tuple[str, ...] = (
    "name", "group_id", "image_id", "anchor_condition_id", "conditions", "storage_mode",
)
#: 草稿必填键（``group_id``/``storage_mode`` 可省）。
DRAFT_REQUIRED: Tuple[str, ...] = ("name", "image_id", "anchor_condition_id", "conditions")
#: 资产存储模式枚举（02）。
STORAGE_MODES: Tuple[str, ...] = ("original", "png_lossless")

#: 占位 ImageAsset（仅用于经 ``validate_target`` 复用 name/条件树/anchor 规则；真实资产
#: 由组合根的 ``TargetImageResolver`` 另行物化）。与 ``targets.domain._PLACEHOLDER_IMAGE`` 同形，
#: 此处**本地**复刻以避免 Vision→Targets 依赖（守 00 允许依赖表）。
_PLACEHOLDER_IMAGE: Dict[str, Any] = {
    "path": "images/" + ("0" * 64) + ".png",
    "sha256": "0" * 64,
    "width": 1,
    "height": 1,
    "format": "png",
    "storage_mode": "png_lossless",
}


@dataclass(frozen=True)
class DraftSpec:
    """规范化草稿（03 TargetDraft；不含 id/编号/revision/时间）。"""

    name: str
    image_id: str
    anchor_condition_id: str
    conditions: Any


def _draft_target_placeholder(*, name: Any, conditions: Any, anchor_condition_id: Any,
                              group_id: Any) -> Dict[str, Any]:
    """构造字段齐全的完整目标占位 dict，供 ``validate_target`` 复用权威规则（占位手法）。"""
    return {
        "id": "draft-placeholder",
        "sample_no": 1,
        "revision": 1,
        "name": name,
        "group_id": group_id,
        "image": dict(_PLACEHOLDER_IMAGE),
        "thumbnail_path": None,
        "anchor_condition_id": anchor_condition_id,
        "conditions": conditions,
        "created_at": "2000-01-01T00:00:00Z",
        "updated_at": "2000-01-01T00:00:00Z",
        "metadata": {},
    }


def validate_draft(draft: Any) -> DraftSpec:
    """校验 ``TargetDraft``（03）并返回规范化值。

    **复用** ``types.conditions.validate_target``（占位手法，与 ``targets.domain.validate_draft``
    同源）：name（去首尾空白 1–100 码点）、条件树（叶数/深度/唯一性/type/params/roi）、
    anchor 存在性均由权威校验器裁决；本函数只做草稿**键集/必填/枚举**的前置约束。

    Raises:
        TypeValidationError: 键集/必填/类型/name/条件树/anchor 非法（06 四字段）。
    """
    if not isinstance(draft, Mapping):
        raise TypeValidationError("INVALID_PARAMS", "draft", "draft 必须为对象")
    for key in draft:
        if key not in DRAFT_KEYS:
            raise TypeValidationError(
                "INVALID_PARAMS", str(key), "TargetDraft 未声明字段: %s" % key
            )
    # storage_mode 单独出现而无 image_id → 定位 storage_mode（与 targets.domain Q7 一致）。
    if "storage_mode" in draft and "image_id" not in draft:
        raise TypeValidationError(
            "INVALID_PARAMS", "storage_mode", "storage_mode 必须与 image_id 同时提供"
        )
    for key in DRAFT_REQUIRED:
        if key not in draft:
            raise TypeValidationError(
                "INVALID_PARAMS", key, "TargetDraft 缺少必填字段: %s" % key
            )
    image_id = draft.get("image_id")
    if not isinstance(image_id, str) or not image_id:
        raise TypeValidationError("INVALID_PARAMS", "image_id", "image_id 必须为非空字符串")
    storage_mode = draft.get("storage_mode", STORAGE_MODES[0])
    if storage_mode not in STORAGE_MODES:
        raise TypeValidationError(
            "INVALID_PARAMS", "storage_mode",
            "storage_mode 必须为 %s 之一" % "/".join(STORAGE_MODES),
        )
    group_id = draft.get("group_id")
    if group_id is not None and (not isinstance(group_id, str) or not group_id):
        raise TypeValidationError("INVALID_PARAMS", "group_id", "group_id 必须为字符串或 null")
    # 权威复用：name / 条件树 / anchor 存在性由 validate_target 裁决（占位 image/时间/metadata）。
    normalized = validate_target(_draft_target_placeholder(
        name=draft.get("name"),
        conditions=draft.get("conditions"),
        anchor_condition_id=draft.get("anchor_condition_id"),
        group_id=group_id,
    ))
    return DraftSpec(
        name=normalized.name,
        image_id=image_id,
        anchor_condition_id=normalized.anchor_condition_id,
        conditions=normalized.conditions,
    )


@dataclass(frozen=True)
class LeafSpec:
    """规范化的条件叶（DFS 前序）。"""

    condition_id: str
    type: str
    params: Dict[str, Any]
    roi: Optional[LocalRect]
    order: int
    is_anchor: bool


@dataclass(frozen=True)
class BranchSpec:
    """规范化的分支（``all``/``any`` + 有序子节点）。"""

    op: str
    children: Tuple["NodeSpec", ...]
    order: int


NodeSpec = Union[BranchSpec, LeafSpec]


@dataclass(frozen=True)
class ConditionSpec:
    """规范化条件树 spec。

    Attributes:
        root: 根节点（``BranchSpec`` 或单叶 ``LeafSpec``）。
        leaves: 全部叶（DFS 前序，顺序确定）。
        anchor: anchor 叶（02：整帧产候选）。
        anchor_condition_id: anchor 叶 ID。
    """

    root: NodeSpec
    leaves: Tuple[LeafSpec, ...]
    anchor: LeafSpec
    anchor_condition_id: str


# ---------------------------------------------------------------------------
# 解析 / 校验 / 规范化
# ---------------------------------------------------------------------------

def _walk(node: Any, order_counter: List[int], is_anchor_id: str) -> NodeSpec:
    """递归把（已权威校验的）节点树转为有序 spec（DFS 前序）。"""
    if not isinstance(node, dict):
        # _validate_condition_tree 已保证结构合法；防御性兜底
        raise TypeValidationError("INVALID_PARAMS", "conditions", "条件节点必须为对象")
    if "op" in node or "children" in node:
        order = order_counter[0]
        order_counter[0] += 1
        children = tuple(
            _walk(child, order_counter, is_anchor_id) for child in node.get("children", [])
        )
        return BranchSpec(op=node.get("op"), children=children, order=order)
    order = order_counter[0]
    order_counter[0] += 1
    raw_roi = node.get("roi")
    roi = validate_local_rect(raw_roi, prefix="conditions.roi") if raw_roi is not None else None
    condition_id = node.get("id")
    return LeafSpec(
        condition_id=condition_id,
        type=node.get("type"),
        params=dict(node.get("params") or {}),
        roi=roi,
        order=order,
        is_anchor=(condition_id == is_anchor_id),
    )


def serialize_condition_tree(tree: Any, anchor_condition_id: Any) -> ConditionSpec:
    """解析/校验/规范化条件树 → :class:`ConditionSpec`。

    Raises:
        TypeValidationError: 结构/叶 params/叶数/深度/唯一性/anchor 非法（06 四字段）。
    """
    if not isinstance(anchor_condition_id, str) or not anchor_condition_id:
        raise TypeValidationError(
            "INVALID_PARAMS", "anchor_condition_id", "anchor_condition_id 必须为非空字符串"
        )
    # 权威校验（叶数 1–10 / 深度 ≤4 / 唯一 / 叶 type+params+roi / 分支 op 枚举无 NOT）
    leaf_ids = _validate_condition_tree(tree)
    if anchor_condition_id not in leaf_ids:
        raise TypeValidationError(
            "INVALID_PARAMS",
            "anchor_condition_id",
            "anchor_condition_id 必须指向存在的条件叶",
        )
    root = _walk(tree, [0], anchor_condition_id)
    leaves = _collect_leaves(root)
    anchor = next(leaf for leaf in leaves if leaf.is_anchor)
    return ConditionSpec(
        root=root,
        leaves=leaves,
        anchor=anchor,
        anchor_condition_id=anchor_condition_id,
    )


def _collect_leaves(node: NodeSpec) -> Tuple[LeafSpec, ...]:
    """DFS 前序收集叶（与 spec 构建顺序一致）。"""
    if isinstance(node, LeafSpec):
        return (node,)
    ordered: List[LeafSpec] = []
    for child in node.children:
        ordered.extend(_collect_leaves(child))
    return tuple(ordered)


# ---------------------------------------------------------------------------
# ROI 夹取（02:22；相对候选 bbox，越界夹取，全外 → None）
# ---------------------------------------------------------------------------

def leaf_roi_in_frame(
    candidate_bbox: Rect,
    leaf: LeafSpec,
    frame_width: int,
    frame_height: int,
) -> Optional[LocalRect]:
    """把叶 ROI（相对候选 bbox）夹取为**帧内**图像局部矩形。

    - ``leaf.roi is None`` → 使用整个候选 bbox（候选 bbox 即检测 ROI）；
    - 否则绝对矩形 = 候选 bbox 原点 + 叶 ROI 偏移；
    - 与 ``[0,frame_width]×[0,frame_height]`` 求交；交集为空 → ``None``（该叶判 false，**非错误**）。

    Returns:
        夹取后的 ``LocalRect``（图像局部像素、宽高 ≥1）；完全落帧外 → ``None``。
    """
    fw = int(frame_width)
    fh = int(frame_height)
    if leaf.roi is None:
        x = int(candidate_bbox.x)
        y = int(candidate_bbox.y)
        width = int(candidate_bbox.width)
        height = int(candidate_bbox.height)
    else:
        x = int(candidate_bbox.x) + int(leaf.roi.x)
        y = int(candidate_bbox.y) + int(leaf.roi.y)
        width = int(leaf.roi.width)
        height = int(leaf.roi.height)
    x0 = max(0, min(x, fw))
    y0 = max(0, min(y, fh))
    x1 = max(0, min(x + width, fw))
    y1 = max(0, min(y + height, fh))
    if x1 <= x0 or y1 <= y0:
        return None
    return LocalRect(x=x0, y=y0, width=x1 - x0, height=y1 - y0)


__all__ = [
    "LEAF_TYPES",
    "DRAFT_KEYS",
    "DRAFT_REQUIRED",
    "STORAGE_MODES",
    "DraftSpec",
    "LeafSpec",
    "BranchSpec",
    "NodeSpec",
    "ConditionSpec",
    "serialize_condition_tree",
    "validate_draft",
    "leaf_roi_in_frame",
]
