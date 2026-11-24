# -*- coding: utf-8 -*-
"""捕获端口与值对象（TASK-007，设计 §6）。

可注入端口（单测用替身；生产用 GDI 实现）：

- ``CaptureBackend``：把屏幕物理矩形抓成 **BGR uint8** 原始帧；
- ``DisplayEnumerator``：枚举显示器（``list_displays`` 数据源）；
- ``WindowProvider``（见 window_capture.py）：按 binding_id 解析窗口客户区快照。

内部图像固定 BGR uint8（02）：``RawImage.data`` 为行主序、自上而下、每像素
3 字节（B,G,R）。供应商/UI 适配器自行转换，不在本层出现 RGB/Alpha。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Protocol, runtime_checkable

from ..types.region import DisplayInfo

from .cancel import CancelToken


@dataclass(frozen=True)
class RawImage:
    """一帧原始像素：BGR uint8，行主序、自上而下，``len(data)==width*height*3``。"""

    width: int
    height: int
    data: bytes


@runtime_checkable
class CaptureBackend(Protocol):
    """捕获后端端口：把屏幕物理矩形抓成 BGR 帧。"""

    name: str

    def capture_bgr(
        self, x: int, y: int, width: int, height: int, cancel: CancelToken
    ) -> RawImage:
        """抓取屏幕物理矩形 ``[x,x+w)×[y,y+h)``。

        Raises:
            CaptureError: code=CAPTURE_FAILED（瞬时失败，retryable）等。
            CancelledError: 读取到取消信号。
        """
        ...


@runtime_checkable
class DisplayEnumerator(Protocol):
    """显示器枚举端口（``list_displays`` 数据源；可注入替身）。"""

    def list_displays(self) -> List[DisplayInfo]:
        """返回当前显示器集合（物理像素；ID 在会话内稳定，拓扑变化重新枚举）。"""
        ...


# ---------------------------------------------------------------------------
# 时钟与时间格式（与 backend.ipc.envelope.format_timestamp 同格式，避免跨模块依赖）
# ---------------------------------------------------------------------------

WallClock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def default_wall_clock() -> datetime:
    """默认墙钟：UTC（06：墙钟只作日志；captured_at 用墙钟时间戳）。"""
    return datetime.now(timezone.utc)


def format_utc_timestamp(moment: datetime) -> str:
    """RFC3339 UTC 毫秒精度（如 2026-10-09T12:00:00.000Z）。

    与 ``backend.ipc.envelope.format_timestamp`` 逐字同格式；测试断言二者一致，
    防止格式漂移（本模块有意不 import ipc，保持 M01 对 M08 的零依赖）。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)
