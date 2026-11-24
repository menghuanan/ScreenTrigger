# -*- coding: utf-8 -*-
"""捕获命令接线（TASK-007；03 命令目录「系统、捕获与识别」）。

把 6 条捕获命令注册到 dispatcher（仿 ``backend.config.service.register_config_commands``）：

- ``get_display_list`` / ``capture_region`` / ``capture_full_screen`` /
  ``capture_window`` / ``release_image`` / ``get_image_preview``。

参数/返回字段逐字对齐 02/03；未声明参数一律 ``INVALID_PARAMS`` 并 ``details.field``
定位。捕获命令同步完成（IPC 同步面），取消源为 ``NullCancelToken``；长调用取消语义
由 ``CaptureService`` 的 CancelToken 参数承载（供 Script/Vision 等异步调用方注入）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from ..types.capabilities import TypeValidationError
from ..types.region import DisplayInfo, ImageRef, validate_region

from .errors import CaptureError, invalid_params
from .service import CaptureService

# 各命令允许的 params 键集（白名单外 → INVALID_PARAMS，定位多余字段）
_CAPTURE_COMMAND_PARAMS: Dict[str, Tuple[str, ...]] = {
    "get_display_list": (),
    "capture_region": ("region",),
    "capture_full_screen": ("display_id",),
    "capture_window": ("binding_id",),
    "release_image": ("image_id",),
    "get_image_preview": ("image_id", "max_edge"),
}

# get_image_preview.max_edge 范围（03：1–800，缺省 400）
PREVIEW_MAX_EDGE_DEFAULT = 400
PREVIEW_MAX_EDGE_MIN = 1
PREVIEW_MAX_EDGE_MAX = 800


def display_to_dict(display: DisplayInfo) -> Dict[str, Any]:
    """DisplayInfo → 02 字段 dict（id/rect/dpi_scale/primary）。"""
    return {
        "id": display.id,
        "rect": {
            "x": display.rect.x,
            "y": display.rect.y,
            "width": display.rect.width,
            "height": display.rect.height,
        },
        "dpi_scale": display.dpi_scale,
        "primary": display.primary,
    }


def image_ref_to_dict(image_ref: ImageRef) -> Dict[str, Any]:
    """ImageRef → 02 字段 dict（image_id/width/height/format/sha256）。"""
    return {
        "image_id": image_ref.image_id,
        "width": image_ref.width,
        "height": image_ref.height,
        "format": image_ref.format,
        "sha256": image_ref.sha256,
    }


def _require_str(params: Dict[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise invalid_params(name, "参数 %s 必填且为非空字符串" % name)
    return value


def _optional_int(
    params: Dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    if name not in params:
        return default
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_params(name, "参数 %s 必须为整数" % name)
    if value < minimum or value > maximum:
        raise invalid_params(name, "参数 %s 超出范围 [%d, %d]" % (name, minimum, maximum))
    return value


def register_capture_commands(dispatcher: Any, service: CaptureService) -> None:
    """把捕获命令注册到 dispatcher；捕获/类型错误 → DispatcherError（06 四字段）。"""
    from backend.ipc.dispatcher import DispatcherError

    def _wrap(command: str, handler: Callable[[Dict[str, Any]], Dict[str, Any]]):
        allowed = _CAPTURE_COMMAND_PARAMS[command]

        def _handler(envelope: Dict[str, Any]) -> Dict[str, Any]:
            params = envelope.get("params")
            if not isinstance(params, dict):
                params = {}
            try:
                for key in params:
                    if key not in allowed:
                        raise invalid_params(key, "%s 未声明参数: %s" % (command, key))
                return handler(params)
            except TypeValidationError as exc:
                raise DispatcherError(
                    exc.code,
                    exc.message,
                    details={"field": exc.field},
                    retryable=exc.retryable,
                ) from exc
            except CaptureError as exc:
                raise DispatcherError(
                    exc.code, exc.message, details=exc.details, retryable=exc.retryable
                ) from exc

        return _handler

    def _cmd_get_display_list(_params: Dict[str, Any]) -> Dict[str, Any]:
        return {"displays": [display_to_dict(d) for d in service.list_displays()]}

    def _cmd_capture_region(params: Dict[str, Any]) -> Dict[str, Any]:
        region = validate_region(params.get("region"))
        frame = service.capture(region)
        return {"image": image_ref_to_dict(frame.image_ref), "frame_id": frame.frame_id}

    def _cmd_capture_full_screen(params: Dict[str, Any]) -> Dict[str, Any]:
        display_id = _require_str(params, "display_id")
        frame = service.capture_full_screen(display_id)
        return {"image": image_ref_to_dict(frame.image_ref), "frame_id": frame.frame_id}

    def _cmd_capture_window(params: Dict[str, Any]) -> Dict[str, Any]:
        binding_id = _require_str(params, "binding_id")
        frame = service.capture_window(binding_id)
        return {"image": image_ref_to_dict(frame.image_ref), "frame_id": frame.frame_id}

    def _cmd_release_image(params: Dict[str, Any]) -> Dict[str, Any]:
        image_id = _require_str(params, "image_id")
        service.release_image(image_id)
        return {"ok": True}

    def _cmd_get_image_preview(params: Dict[str, Any]) -> Dict[str, Any]:
        image_id = _require_str(params, "image_id")
        max_edge = _optional_int(
            params, "max_edge", PREVIEW_MAX_EDGE_DEFAULT,
            PREVIEW_MAX_EDGE_MIN, PREVIEW_MAX_EDGE_MAX,
        )
        mime, base64_text = service.image_preview(image_id, max_edge)
        return {"mime": mime, "base64": base64_text}

    dispatcher.register("get_display_list", _wrap("get_display_list", _cmd_get_display_list))
    dispatcher.register("capture_region", _wrap("capture_region", _cmd_capture_region))
    dispatcher.register(
        "capture_full_screen", _wrap("capture_full_screen", _cmd_capture_full_screen)
    )
    dispatcher.register("capture_window", _wrap("capture_window", _cmd_capture_window))
    dispatcher.register("release_image", _wrap("release_image", _cmd_release_image))
    dispatcher.register(
        "get_image_preview", _wrap("get_image_preview", _cmd_get_image_preview)
    )
