# -*- coding: utf-8 -*-
"""坐标/几何纯函数（TASK-007，06 坐标不变量）。

全部为纯函数（无 IO/无全局状态），可独立验证：

- 半开矩形 ``[x,x+w)×[y,y+h)`` 的包含/相交判定；
- 屏幕点必须落在**真实显示器集合**（不能只用虚拟屏总包围盒：显示器之间可能有
  空隙，06 不变量 3）；
- 显示器的选择/成员判定；
- 07 §1 的 UI 局部 DIP → 物理像素换算（先转边界再求宽高，避免少一列像素）。

坐标系：一切为**物理像素**；screen 可负（虚拟桌面），client/image 不可负
（由 ``types.region.validate_region`` 保证）。
"""

from __future__ import annotations

from math import ceil, floor
from typing import List, Optional, Sequence, Tuple

from ..types.region import DisplayInfo, Rect


def _bounds(x: int, y: int, width: int, height: int) -> Tuple[int, int, int, int]:
    """返回半开矩形的 (left, top, right, bottom)；right/bottom 为开区间端点。"""
    return x, y, x + width, y + height


def rect_within(inner: Rect, outer: Rect) -> bool:
    """``inner`` 是否完整落在 ``outer`` 内（半开矩形；边界相等视为包含）。"""
    il, it, ir, ib = _bounds(inner.x, inner.y, inner.width, inner.height)
    ol, ot, orr, ob = _bounds(outer.x, outer.y, outer.width, outer.height)
    return il >= ol and it >= ot and ir <= orr and ib <= ob


def rect_contains_point(rect: Rect, x: int, y: int) -> bool:
    """点 ``(x,y)`` 是否落在半开矩形内（右侧/下侧开区间端点不计入）。"""
    return rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height


def find_display(displays: Sequence[DisplayInfo], display_id: str) -> Optional[DisplayInfo]:
    """按 ID 查找显示器；不存在返回 None（调用方转 DISPLAY_NOT_FOUND）。"""
    for display in displays:
        if display.id == display_id:
            return display
    return None


def display_containing_rect(
    displays: Sequence[DisplayInfo], x: int, y: int, width: int, height: int
) -> Optional[DisplayInfo]:
    """返回完整包含该矩形的显示器（真实集合内）；无则 None。

    注意：不使用虚拟屏包围盒——显示器之间可能有空隙（06 不变量 3）。
    """
    target = Rect(x=x, y=y, width=width, height=height)
    for display in displays:
        if rect_within(target, display.rect):
            return display
    return None


def display_at_point(displays: Sequence[DisplayInfo], x: int, y: int) -> Optional[DisplayInfo]:
    """返回包含点 ``(x,y)`` 的显示器；落在空隙/集合外返回 None。"""
    for display in displays:
        if rect_contains_point(display.rect, x, y):
            return display
    return None


def local_dip_rect_to_physical(
    a: Tuple[float, float],
    b: Tuple[float, float],
    origin: Tuple[int, int],
    scale: float,
) -> Rect:
    """UI 局部 DIP 两点 → 物理像素 rect（07 §1）。

    先按 scale 换算边界并取 floor/ceil，再用差值求宽高（避免边缘少一列像素）；
    正反拖动通过 min/max 归一化，对调用方无方向假设。
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    left = origin[0] + floor(min(a[0], b[0]) * scale)
    top = origin[1] + floor(min(a[1], b[1]) * scale)
    right = origin[0] + ceil(max(a[0], b[0]) * scale)
    bottom = origin[1] + ceil(max(a[1], b[1]) * scale)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)


def merge_displays_bounds(displays: Sequence[DisplayInfo]) -> Optional[Rect]:
    """虚拟屏总包围盒（仅诊断/日志用；**禁止**用于区域合法性校验，06 不变量 3）。"""
    if not displays:
        return None
    left = min(d.rect.x for d in displays)
    top = min(d.rect.y for d in displays)
    right = max(d.rect.x + d.rect.width for d in displays)
    bottom = max(d.rect.y + d.rect.height for d in displays)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)


def display_ids(displays: Sequence[DisplayInfo]) -> List[str]:
    """显示器 ID 列表（保序；诊断用）。"""
    return [d.id for d in displays]
