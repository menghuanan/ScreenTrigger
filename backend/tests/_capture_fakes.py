# -*- coding: utf-8 -*-
"""TASK-007 单测共享替身（不依赖真实屏幕/窗口/输入）。

提供可注入端口的内存替身：
- ``FakeDisplayEnumerator`` / ``FakeCaptureBackend`` / ``FakeWindowProvider``；
- ``FakeClock`` / ``RecordingSleeper`` / ``SimpleCancelToken``；
- dispatcher 直连用的 ``CaptureTransport`` / ``SessionStub``。

真实 OS 路径由 reports/TASK-007 的真实捕获记录承担；单测只用替身（执行约定：
单测使用时钟/帧/输入端口替身，禁止依赖真实屏幕）。
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.capture.cancel import CancelledError
from backend.capture.window_capture import WindowSnapshot
from backend.capture.errors import window_not_found
from backend.capture.ports import RawImage
from backend.image.store import SessionImageStore
from backend.capture.service import CaptureService
from backend.types.region import DisplayInfo, Rect

FIXED_MOMENT = datetime(2026, 10, 9, 12, 0, 5, tzinfo=timezone.utc)


def display(
    display_id: str, x: int, y: int, width: int, height: int,
    dpi_scale: float = 1.0, primary: bool = False,
) -> DisplayInfo:
    """构造 DisplayInfo（物理 rect）。"""
    return DisplayInfo(
        id=display_id,
        rect=Rect(x=x, y=y, width=width, height=height),
        dpi_scale=dpi_scale,
        primary=primary,
    )


class FakeDisplayEnumerator:
    """内存显示器枚举（记录调用次数；可动态变更集合模拟显示器拔除）。"""

    def __init__(self, displays: List[DisplayInfo]) -> None:
        self.displays = list(displays)
        self.calls = 0

    def list_displays(self) -> List[DisplayInfo]:
        self.calls += 1
        return list(self.displays)


class FakeCaptureBackend:
    """脚本化捕获后端：按序返回 RawImage 或抛异常，记录每次调用的坐标。"""

    name = "fake"

    def __init__(self, results: Optional[List[Any]] = None) -> None:
        self.calls: List[tuple] = []
        self._results: List[Any] = list(results or [])

    def capture_bgr(self, x, y, width, height, cancel) -> RawImage:
        cancel.throw_if_cancelled()
        self.calls.append((x, y, width, height))
        if self._results:
            item = self._results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return solid_image(width, height)


def solid_image(width: int, height: int, bgr=(10, 20, 30)) -> RawImage:
    """构造纯色 BGR 帧（确定性内容，便于断言 sha256）。"""
    return RawImage(width=width, height=height, data=bytes(bgr) * (width * height))


class FakeWindowProvider:
    """内存窗口端口：命中返回快照，缺失抛 WINDOW_NOT_FOUND（记录调用）。"""

    def __init__(self, snapshots: Optional[Dict[str, WindowSnapshot]] = None) -> None:
        self.snapshots = dict(snapshots or {})
        self.calls: List[str] = []

    def snapshot(self, binding_id: str) -> WindowSnapshot:
        self.calls.append(binding_id)
        snapshot = self.snapshots.get(binding_id)
        if snapshot is None:
            raise window_not_found(binding_id)
        return snapshot


def window_snapshot(
    binding_id: str, x: int, y: int, width: int, height: int,
    geometry_revision: int = 1, visible: bool = True, minimized: bool = False,
    foreground: bool = True,
) -> WindowSnapshot:
    """构造窗口客户区快照（客户端物理 rect）。"""
    return WindowSnapshot(
        binding_id=binding_id,
        client_rect=Rect(x=x, y=y, width=width, height=height),
        geometry_revision=geometry_revision,
        visible=visible,
        minimized=minimized,
        foreground=foreground,
    )


class FakeClock:
    """固定墙钟（captured_at 可断言）。"""

    def __init__(self, moment: datetime = FIXED_MOMENT) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class RecordingSleeper:
    """记录重试等待时长、不做真实休眠（可选 on_sleep 回调模拟等待中事件）。"""

    def __init__(self, on_sleep=None) -> None:
        self.delays: List[float] = []
        self.on_sleep = on_sleep

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(seconds)


class SimpleCancelToken:
    """手动取消令牌（线程无关；单测同步语义）。"""

    def __init__(self) -> None:
        self.cancelled = False
        self.checks = 0

    def cancel(self) -> None:
        self.cancelled = True

    def is_cancelled(self) -> bool:
        return self.cancelled

    def throw_if_cancelled(self) -> None:
        self.checks += 1
        if self.cancelled:
            raise CancelledError("操作已取消")


def make_store(session_id: str = "s-test", budget_bytes: Optional[int] = None,
               image_id_factory=None) -> SessionImageStore:
    return SessionImageStore(
        session_id, budget_bytes=budget_bytes, image_id_factory=image_id_factory
    )


def make_service(
    displays: Optional[List[DisplayInfo]] = None,
    backend: Optional[FakeCaptureBackend] = None,
    window_provider: Optional[FakeWindowProvider] = None,
    store: Optional[SessionImageStore] = None,
    clock=None,
    sleeper=None,
    frame_id_factory=None,
    background=None,
) -> CaptureService:
    """组装用替身端口的 CaptureService。"""
    if displays is None:
        displays = [display("display-1", 0, 0, 1920, 1080, primary=True)]
    ids = iter("frame-%d" % n for n in range(1, 10000))
    return CaptureService(
        backend=backend if backend is not None else FakeCaptureBackend(),
        display_enumerator=FakeDisplayEnumerator(displays),
        window_provider=window_provider if window_provider is not None else FakeWindowProvider(),
        image_store=store if store is not None else make_store(),
        clock=clock if clock is not None else FakeClock(),
        sleeper=sleeper if sleeper is not None else RecordingSleeper(),
        frame_id_factory=frame_id_factory if frame_id_factory is not None else (lambda: next(ids)),
        background=background,
    )


# ---------------------------------------------------------------------------
# dispatcher 直连替身（命令接线测试）
# ---------------------------------------------------------------------------

class CaptureTransport:
    """内存传输替身：send 捕获响应信封，read_line 立即 EOF。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(self, envelope: Dict[str, Any]) -> bool:
        self.sent.append(envelope)
        return True

    def read_line(self):
        return None


class SessionStub:
    session_id = "s-test"


def quiet_logger():
    lines: List[str] = []

    def _log(message: str) -> None:
        lines.append(message)

    return _log, lines


def silent_stderr() -> io.StringIO:
    return io.StringIO()
