# -*- coding: utf-8 -*-
"""空间类型（TASK-006 批 B1，设计 §3.1/§6.1；schema/types.schema.json 同源）。

02 权威规则：
- Rect：x/y 任意整数，width/height ≥1（左闭右开 [x,x+w)×[y,y+h)）；
- Point：单像素读取，不受 Region ≥10 限制，坐标可负（虚拟桌面）；
- LocalRect：条件局部 ROI，宽高 ≥1（不受 10×10 约束）；
- Region：space 三枚举；空间互斥（display_id 仅 screen、binding_id 仅 client、
  image_id 仅 image，出现即违例含 null）；screen x/y 可负，client/image 不可负；
  width/height ≥10（10×10 下限）→ INVALID_REGION（06"太小"分支）；
- DisplayInfo / ImageRef / ImageAsset / Frame 逐字段照 02。

全部 validate_* 为纯函数：无 IO、无全局状态，错误统一抛
TypeValidationError（code=06 错误码，details.field=逐字字段路径）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .capabilities import (
    REGIONS_SPACES,
    bool_field,
    check_keys,
    fail,
    int_field,
    number_field,
    relative_path_field,
    require_object,
    str_field,
    hex64_field,
    enum_field,
)

_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$")
_IMAGE_FORMATS = ("png", "jpeg")
_STORAGE_MODES = ("original", "png_lossless")


def validate_rfc3339_utc(value: Any, field: str) -> str:
    """02：UTC RFC3339（毫秒可选），如 2026-09-21T09:00:00Z。"""
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        fail("INVALID_PARAMS", field, "%s 必须为 RFC3339 UTC 时间字符串" % field)
    return value


# ---------------------------------------------------------------------------
# 基础空间类型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    """02：不自动携带空间；DisplayInfo.rect 等场景使用。"""

    x: int
    y: int
    width: int
    height: int


def validate_rect(value: Any) -> Rect:
    data = require_object(value, "rect")
    check_keys(data, ("x", "y", "width", "height"), "rect")
    return Rect(
        x=int_field(data, "x", "rect.x"),
        y=int_field(data, "y", "rect.y"),
        width=int_field(data, "width", "rect.width", minimum=1),
        height=int_field(data, "height", "rect.height", minimum=1),
    )


@dataclass(frozen=True)
class Point:
    """02：单像素读取，不受 Region ≥10 限制；坐标可负。"""

    x: int
    y: int


def validate_point(value: Any) -> Point:
    data = require_object(value, "point")
    check_keys(data, ("x", "y"), "point")
    return Point(x=int_field(data, "x", "point.x"), y=int_field(data, "y", "point.y"))


@dataclass(frozen=True)
class LocalRect:
    """02：条件局部 ROI（相对候选 bbox），宽高≥1，不用 Region。"""

    x: int
    y: int
    width: int
    height: int


def validate_local_rect(value: Any, prefix: str = "local_rect") -> LocalRect:
    data = require_object(value, prefix)
    check_keys(data, ("x", "y", "width", "height"), prefix)
    return LocalRect(
        x=int_field(data, "x", prefix + ".x"),
        y=int_field(data, "y", prefix + ".y"),
        width=int_field(data, "width", prefix + ".width", minimum=1),
        height=int_field(data, "height", prefix + ".height", minimum=1),
    )


# ---------------------------------------------------------------------------
# Region（§6.1 全规则）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    """02：三空间互斥；screen 可负；宽高≥10（10×10 下限，Point 豁免）。"""

    space: str
    x: int
    y: int
    width: int
    height: int
    display_id: Optional[str] = None
    binding_id: Optional[str] = None
    image_id: Optional[str] = None


# space → (必填锚字段, 其余禁入锚字段)
_REGION_ANCHORS: Dict[str, str] = {
    "screen_physical": "display_id",
    "window_client_physical": "binding_id",
    "image_pixel": "image_id",
}


def validate_region(value: Any) -> Region:
    """空间互斥/负坐标口径/10×10 下限三端同语义（契约用例 type-region-*）。"""
    data = require_object(value, "region")
    check_keys(
        data,
        ("space", "x", "y", "width", "height", "display_id", "binding_id", "image_id"),
        "region",
    )
    space = enum_field(data, "space", "region.space", REGIONS_SPACES)

    required_anchor = _REGION_ANCHORS[space]
    for other_space, anchor in _REGION_ANCHORS.items():
        if anchor == required_anchor:
            continue
        # 空间互斥：他空间锚字段出现即违例（含 null），定位到该字段
        if anchor in data:
            fail("INVALID_PARAMS", "region.%s" % anchor,
                 "region.%s 仅 %s 空间允许出现" % (anchor, other_space))

    minimum = None if space == "screen_physical" else 0
    x = int_field(data, "x", "region.x", minimum=minimum)
    y = int_field(data, "y", "region.y", minimum=minimum)
    # 10×10 下限 → INVALID_REGION（06 "太小"分支）；Point 豁免不在此函数
    width = int_field(data, "width", "region.width", minimum=10, code="INVALID_REGION")
    height = int_field(data, "height", "region.height", minimum=10, code="INVALID_REGION")

    anchor_value = str_field(data, required_anchor, "region.%s" % required_anchor)
    kwargs: Dict[str, Any] = {
        "display_id": None,
        "binding_id": None,
        "image_id": None,
    }
    kwargs[required_anchor] = anchor_value
    return Region(space=space, x=x, y=y, width=width, height=height, **kwargs)


# ---------------------------------------------------------------------------
# 显示器 / 图像 / 帧
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DisplayInfo:
    """02：rect 为 screen 物理坐标；dpi_scale>0。"""

    id: str
    rect: Rect
    dpi_scale: float
    primary: bool


def validate_display_info(value: Any) -> DisplayInfo:
    data = require_object(value, "display_info")
    check_keys(data, ("id", "rect", "dpi_scale", "primary"), "display_info")
    return DisplayInfo(
        id=str_field(data, "id", "display_info.id"),
        rect=validate_rect(data.get("rect")),
        dpi_scale=number_field(data, "dpi_scale", "display_info.dpi_scale",
                               exclusive_minimum=True),
        primary=bool_field(data, "primary", "display_info.primary"),
    )


@dataclass(frozen=True)
class ImageRef:
    """02：引用只在当前引擎会话有效；持久化转 ImageAsset。"""

    image_id: str
    width: int
    height: int
    format: str
    sha256: str


def validate_image_ref(value: Any) -> ImageRef:
    data = require_object(value, "image_ref")
    check_keys(data, ("image_id", "width", "height", "format", "sha256"), "image_ref")
    return ImageRef(
        image_id=str_field(data, "image_id", "image_ref.image_id"),
        width=int_field(data, "width", "image_ref.width"),
        height=int_field(data, "height", "image_ref.height"),
        format=enum_field(data, "format", "image_ref.format", _IMAGE_FORMATS),
        sha256=hex64_field(data, "sha256", "image_ref.sha256"),
    )


@dataclass(frozen=True)
class ImageAsset:
    """02：持久化图像资产；path 为数据根相对路径（禁绝对/盘符/..）。"""

    path: str
    sha256: str
    width: int
    height: int
    format: str
    storage_mode: str


def validate_image_asset(value: Any) -> ImageAsset:
    data = require_object(value, "image")
    check_keys(
        data, ("path", "sha256", "width", "height", "format", "storage_mode"), "image"
    )
    return ImageAsset(
        path=relative_path_field(data, "path", "image.path"),
        sha256=hex64_field(data, "sha256", "image.sha256"),
        width=int_field(data, "width", "image.width"),
        height=int_field(data, "height", "image.height"),
        format=enum_field(data, "format", "image.format", _IMAGE_FORMATS),
        storage_mode=enum_field(data, "storage_mode", "image.storage_mode",
                                _STORAGE_MODES),
    )


@dataclass(frozen=True)
class Frame:
    """02：未绑定 geometry_revision=0、binding_id=null；图像导入 origin=(0,0)。"""

    frame_id: str
    image_ref: ImageRef
    space: str
    origin: Point
    captured_at: str
    geometry_revision: int
    binding_id: Optional[str]


def validate_frame(value: Any) -> Frame:
    data = require_object(value, "frame")
    check_keys(
        data,
        ("frame_id", "image_ref", "space", "origin", "captured_at",
         "geometry_revision", "binding_id"),
        "frame",
    )
    binding_id = data.get("binding_id")
    if binding_id is not None and not isinstance(binding_id, str):
        fail("INVALID_PARAMS", "frame.binding_id",
             "frame.binding_id 必须为字符串或 null")
    return Frame(
        frame_id=str_field(data, "frame_id", "frame.frame_id"),
        image_ref=validate_image_ref(data.get("image_ref")),
        space=enum_field(data, "space", "frame.space", REGIONS_SPACES),
        origin=validate_point(data.get("origin")),
        captured_at=validate_rfc3339_utc(data.get("captured_at"), "frame.captured_at"),
        geometry_revision=int_field(data, "geometry_revision",
                                    "frame.geometry_revision", minimum=0),
        binding_id=binding_id,
    )
