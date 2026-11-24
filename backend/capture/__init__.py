# -*- coding: utf-8 -*-
"""backend.capture：捕获适配器与帧生命周期（TASK-007，M01）。

模块边界（01）：

- 允许依赖窗口几何端口（WindowProvider）；**不实现** M06（窗口身份/绑定归
  TASK-018/Rust）；捕获流程**不调用任何 Input/SendInput**；
- 生产缺省后端为 Windows **GDI/BitBlt**（用户 2026 确认）；WGC 作为后续可插拔
  后端，本轮不实现、不写无法验证的 Rust COM；
- 全部端口（backend / display_enumerator / window_provider / clock / sleeper）
  可注入，单测用替身，**不依赖真实屏幕**。

本 barrel 只 import 纯 Python 模块（无 ctypes）；真实 GDI 后端仅在
``build_default_capture_service`` 内延迟 import，故非 Windows/无 API 时导入本包
不受影响，捕获能力保持 unavailable。
"""

from __future__ import annotations

from typing import Callable, Optional

from ..image.store import SessionImageStore

from .cancel import (
    CancelFlag,
    CancelToken,
    CancelledError,
    NullCancelToken,
)
from .commands import (
    display_to_dict,
    image_ref_to_dict,
    register_capture_commands,
)
from .errors import CaptureError
from .geometry import (
    display_at_point,
    display_containing_rect,
    find_display,
    local_dip_rect_to_physical,
    merge_displays_bounds,
    rect_contains_point,
    rect_within,
)
from .ports import (
    CaptureBackend,
    DisplayEnumerator,
    RawImage,
    default_wall_clock,
    format_utc_timestamp,
)
from .service import RETRY_DELAY_SECONDS, CaptureService
from .window_capture import NullWindowProvider, WindowProvider, WindowSnapshot

__all__ = [
    "RETRY_DELAY_SECONDS",
    "CancelFlag",
    "CancelToken",
    "CancelledError",
    "CaptureBackend",
    "CaptureError",
    "CaptureService",
    "DisplayEnumerator",
    "NullCancelToken",
    "NullWindowProvider",
    "RawImage",
    "WindowProvider",
    "WindowSnapshot",
    "build_default_capture_service",
    "default_wall_clock",
    "display_at_point",
    "display_containing_rect",
    "display_to_dict",
    "find_display",
    "format_utc_timestamp",
    "image_ref_to_dict",
    "local_dip_rect_to_physical",
    "merge_displays_bounds",
    "rect_contains_point",
    "rect_within",
    "register_capture_commands",
]


def build_default_capture_service(
    session_id: str,
    clock: Optional[Callable] = None,
    sleeper: Optional[Callable] = None,
    logger: Optional[Callable[[str], None]] = None,
    image_store: Optional[SessionImageStore] = None,
) -> CaptureService:
    """装配生产捕获服务（GDI/BitBlt 后端 + Per-Monitor V2 DPI）。

    Raises:
        Exception: 非 Windows 或缺 API 时（如 ctypes.windll 不存在），调用方
            （main.py）捕获后保持 capture 能力 unavailable。
    """
    # 延迟 import：避免在非 Windows 环境 import 本包即失败。
    from .gdi import GdiCaptureBackend, GdiDisplayEnumerator, ensure_per_monitor_v2

    ensure_per_monitor_v2()
    store = image_store if image_store is not None else SessionImageStore(
        session_id=session_id, logger=logger
    )
    return CaptureService(
        backend=GdiCaptureBackend(),
        display_enumerator=GdiDisplayEnumerator(),
        window_provider=NullWindowProvider(),
        image_store=store,
        clock=clock,
        sleeper=sleeper,
        logger=logger,
    )
