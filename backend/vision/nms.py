# -*- coding: utf-8 -*-
"""候选去重与截断纯函数（TASK-013 §8.2；无 IO / 无状态）。

02：模板候选做 NMS（IoU>0.5 保留高置信）……轮廓候选**按相同规则**去重；最多返回
100 个候选，标 ``truncated=true``。基座做**最终统一** NMS + 截断（幂等：已去重输入
再 NMS 不变），保证三算法语义一致、便于单点契约测试。

纯函数：入参为具备 ``bbox``/``confidence`` 属性的候选（``backend.vision.types.Candidate``）；
``Rect`` 为左闭右开 ``[x,x+w)×[y,y+h)``（02）。
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

#: NMS 交并比抑制阈值（02：IoU > 0.5 抑制低分者）。
IOU_THRESHOLD: float = 0.5

#: 单次结果最大候选数（02：最多 100，超出标 ``truncated=true``）。
MAX_CANDIDATES: int = 100


def _area(rect: Any) -> int:
    return int(rect.width) * int(rect.height)


def _intersection_area(a: Any, b: Any) -> int:
    left = max(int(a.x), int(b.x))
    top = max(int(a.y), int(b.y))
    right = min(int(a.x) + int(a.width), int(b.x) + int(b.width))
    bottom = min(int(a.y) + int(a.height), int(b.y) + int(b.height))
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def iou(a: Any, b: Any) -> float:
    """两个矩形（``bbox``）的交并比；并集面积为 0 → 0.0。"""
    inter = _intersection_area(a, b)
    if inter == 0:
        return 0.0
    union = _area(a) + _area(b) - inter
    if union <= 0:
        return 0.0
    return inter / union


def nms(candidates: Sequence[Any], threshold: float = IOU_THRESHOLD) -> List[Any]:
    """非极大值抑制：按 ``confidence`` 降序，抑制 IoU > threshold 的低分候选。

    确定性：同分按输入顺序稳定（``sorted`` 稳定排序）。
    """
    ordered = sorted(candidates, key=lambda c: float(c.confidence), reverse=True)
    kept: List[Any] = []
    for candidate in ordered:
        suppressed = False
        for winner in kept:
            if iou(candidate.bbox, winner.bbox) > threshold:
                suppressed = True
                break
        if not suppressed:
            kept.append(candidate)
    return kept


def cap_candidates(
    candidates: Sequence[Any], limit: int = MAX_CANDIDATES
) -> Tuple[Tuple[Any, ...], bool]:
    """截断到 ``limit`` 个；返回 ``(kept_tuple, truncated)``（02：超限标 truncated）。"""
    if limit < 1:
        raise ValueError("limit 必须 ≥1")
    truncated = len(candidates) > limit
    kept = tuple(candidates[:limit])
    return kept, truncated
