# -*- coding: utf-8 -*-
"""CaptureService：显示器枚举、物理区域/客户区捕获与 Frame 生命周期（TASK-007）。

职责（M01，01/02/06）：

- ``list_displays``：转发可注入的 ``DisplayEnumerator``（生产为 GDI 实现）；
- ``capture``：把 ``Region`` 抓成 ``Frame``，覆盖 screen_physical 与
  window_client_physical 两个空间；
- ``capture_full_screen`` / ``capture_window``：单显示器全屏 / 绑定窗口客户区的
  便捷入口；
- ``release_image`` / ``image_preview``：委托会话级 ImageStore。

关键不变量：

- **物理像素、半开矩形** ``[x,x+w)×[y,y+h)``；screen 可负、client 不可负；
- 区域必须落在**真实显示器集合**内（不用虚拟屏总包围盒，06 不变量 3）；越界/
  太小/空间不匹配 → ``INVALID_REGION``；显示器消失 → ``DISPLAY_NOT_FOUND``；
- **窗口客户区**以 ClientToScreen 原点为准（含标题栏的 GetWindowRect 不用）；
  无效绑定 → ``WINDOW_NOT_FOUND``，最小化/不可见 → ``WINDOW_UNAVAILABLE``；
- **捕获转换为 screen_physical**（00：窗口 ROI 捕获时转 screen_physical 并带
  geometry_revision）；未绑定 ``geometry_revision=0``、``binding_id=null``；
- **一次性 200ms 重试**：仅瞬时 ``CAPTURE_FAILED`` 自动重试一次（06）；OCR/参数/
  存储/键鼠不重试（本层只可能遇 CAPTURE_FAILED）；
- **协作式取消**：长操作接收 ``CancelToken`` 并在点位检查（01）；
- **不调用任何 Input/SendInput**：捕获流程无输入端口，结构上不可能发输入。
"""

from __future__ import annotations

from dataclasses import replace
import time
from typing import Callable, List, Optional

from ..image.store import FrameMeta, SessionImageStore
from ..types.region import DisplayInfo, Frame, ImageRef, Point, Rect, Region

from .cancel import CancelToken, NullCancelToken
from .errors import (
    CaptureError,
    capture_failed,
    display_not_found,
    invalid_region,
    window_not_found,
    window_unavailable,
)
from .geometry import find_display, rect_within
from .ports import (
    CaptureBackend,
    DisplayEnumerator,
    RawImage,
    Sleeper,
    WallClock,
    default_wall_clock,
    format_utc_timestamp,
)
from .window_capture import WindowProvider, WindowSnapshot

# 抓取区域最小边长（与 Region 的 10×10 下限一致，06）
MIN_REGION_EDGE = 10
# 瞬时捕获失败自动重试间隔（06：只自动重试一次 200ms）
RETRY_DELAY_SECONDS = 0.2

Logger = Callable[[str], None]


class CaptureService:
    """捕获服务（端口全注入；单测用替身，生产用 GDI 实现）。"""

    def __init__(
        self,
        backend: CaptureBackend,
        display_enumerator: DisplayEnumerator,
        window_provider: WindowProvider,
        image_store: SessionImageStore,
        clock: Optional[WallClock] = None,
        sleeper: Optional[Sleeper] = None,
        logger: Optional[Logger] = None,
        frame_id_factory: Optional[Callable[[], str]] = None,
        retry_delay_seconds: float = RETRY_DELAY_SECONDS,
        background=None,
    ) -> None:
        self._backend = backend
        self._enumerator = display_enumerator
        self._window_provider = window_provider
        self._store = image_store
        self._clock: WallClock = clock if clock is not None else default_wall_clock
        self._sleeper: Sleeper = sleeper if sleeper is not None else time.sleep
        self._logger = logger
        self._frame_id_factory = frame_id_factory or self._default_frame_id
        self._retry_delay_seconds = retry_delay_seconds
        self._background = background

    @staticmethod
    def _default_frame_id() -> str:
        import uuid

        return "frame-" + uuid.uuid4().hex

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    @property
    def image_store(self) -> SessionImageStore:
        """会话级图像注册表（release_image/get_image_preview 的数据源）。"""
        return self._store

    # -- 显示器 ---------------------------------------------------------------

    def attach_window_provider(self, provider: WindowProvider) -> None:
        """Install the session host at composition time, before serving requests."""
        self._window_provider = provider

    def list_displays(self) -> List[DisplayInfo]:
        """当前显示器集合（物理像素；拓扑变化重新枚举）。"""
        return list(self._enumerator.list_displays())

    def window_capabilities(self, binding):
        window = self._window_provider.snapshot(binding)
        reason = None
        if self._background is None or window.minimized or not window.visible:
            capture = {"non_foreground": "unavailable", "fully_occluded": "unavailable"}
            reason = "后台绘制适配器不可用" if self._background is None else "窗口已最小化或不可见"
        else:
            capture = self._background.capabilities(binding)
            reason = self._background.unavailable_reason(binding)
        return {"binding_id": binding, "capture": capture, "reason": reason}

    def run_region(self, display_id, binding_id, *, window_mode="foreground"):
        """Resolve the current full display or client area in its declared space."""
        self._check_mode(window_mode, binding_id)
        if binding_id:
            rect = self._resolve_window(binding_id, window_mode).client_rect
            return Region("window_client_physical", 0, 0, rect.width, rect.height,
                          binding_id=binding_id)
        display = find_display(self.list_displays(), display_id)
        if display is None:
            raise display_not_found(display_id)
        rect = display.rect
        return Region("screen_physical", rect.x, rect.y, rect.width, rect.height,
                      display_id=display.id)

    def capture_point(self, x, y, region, cancel, *, window_mode="foreground"):
        """Read one physical pixel with the same identity checks as a whole frame."""
        self._check_mode(window_mode, region.binding_id)
        if region.binding_id:
            snapshot = self._resolve_window(region.binding_id, window_mode)
            left, top = snapshot.client_rect.x + region.x, snapshot.client_rect.y + region.y
            valid = lambda: self._check_window(snapshot, window_mode)
            bounds = Rect(snapshot.client_rect.x, snapshot.client_rect.y,
                          snapshot.client_rect.width, snapshot.client_rect.height)
        else:
            display = find_display(self.list_displays(), region.display_id)
            if display is None:
                raise display_not_found(region.display_id)
            left, top, bounds = region.x, region.y, display.rect
            valid = lambda: self._check_display(display)
        if not (left <= x < left + region.width and top <= y < top + region.height
                and bounds.x <= x < bounds.x + bounds.width and bounds.y <= y < bounds.y + bounds.height):
            raise invalid_region("point", "像素位于有效识别区之外")
        cancel.throw_if_cancelled()
        if window_mode == "background":
            raw = self._background.read(snapshot, x-bounds.x, y-bounds.y, 1, 1, cancel)
        else:
            raw = self._capture_with_retry(x, y, 1, 1, cancel, valid)
        valid()
        cancel.throw_if_cancelled()
        if (raw.width, raw.height, len(raw.data)) != (1, 1, 3):
            raise capture_failed("像素读取尺寸错误", retryable=False)
        b, g, r = raw.data
        return {"r": r, "g": g, "b": b}

    # -- 捕获 -----------------------------------------------------------------

    def capture(self, region: Region, cancel: Optional[CancelToken] = None, *, window_mode="foreground") -> Frame:
        """捕获一个区域为 Frame。

        screen_physical：校验区域落在其 ``display_id`` 对应的真实显示器内；
        window_client_physical：经 WindowProvider 解析客户区，区域不得越出客户区，
        捕获坐标换算为屏幕物理（ClientToScreen 原点 + 客户区偏移）。

        Raises:
            CaptureError: INVALID_REGION / DISPLAY_NOT_FOUND / WINDOW_NOT_FOUND /
                WINDOW_UNAVAILABLE / CAPTURE_FAILED / RESOURCE_LIMIT。
            CancelledError: 读取到取消信号。
        """
        token: CancelToken = cancel if cancel is not None else NullCancelToken()
        token.throw_if_cancelled()
        self._validate_region_shape(region)
        self._check_mode(window_mode, region.binding_id)

        if region.space == "screen_physical":
            display = find_display(self.list_displays(), region.display_id or "")
            if display is None:
                raise display_not_found(region.display_id or "")
            if not rect_within(region, display.rect):
                raise invalid_region(
                    "region",
                    "区域越出显示器 %s 的有效范围（不自动裁剪）" % display.id,
                )
            return self._capture_physical(
                x=region.x,
                y=region.y,
                width=region.width,
                height=region.height,
                origin=Point(region.x, region.y),
                geometry_revision=0,
                binding_id=None,
                cancel=token,
                validate=lambda: self._check_display(display),
            )

        # window_client_physical
        snapshot = self._resolve_window(region.binding_id or "", window_mode)
        # 客户区 region 为**客户区局部坐标**（原点为客户区左上，02）；不得越出
        # 客户区尺寸（client_rect 的位置是屏幕坐标，仅用于换算，不参与局部包含）。
        if (
            region.x < 0
            or region.y < 0
            or region.x + region.width > snapshot.client_rect.width
            or region.y + region.height > snapshot.client_rect.height
        ):
            raise invalid_region("region", "区域越出窗口客户区（不自动裁剪）")
        screen_x = snapshot.client_rect.x + region.x
        screen_y = snapshot.client_rect.y + region.y
        read = None
        if window_mode == "background":
            read = lambda: self._background.read(snapshot, region.x, region.y, region.width, region.height, token)
        return self._capture_physical(
            x=screen_x,
            y=screen_y,
            width=region.width,
            height=region.height,
            origin=Point(screen_x, screen_y),
            geometry_revision=snapshot.geometry_revision,
            binding_id=snapshot.binding_id,
            cancel=token,
            validate=lambda: self._check_window(snapshot, window_mode),
            read=read,
        )

    def capture_full_screen(
        self, display_id: str, cancel: Optional[CancelToken] = None
    ) -> Frame:
        """捕获单个显示器全屏。

        Raises:
            CaptureError: DISPLAY_NOT_FOUND（显示器消失）。
        """
        token: CancelToken = cancel if cancel is not None else NullCancelToken()
        token.throw_if_cancelled()
        display = find_display(self.list_displays(), display_id)
        if display is None:
            raise display_not_found(display_id)
        rect = display.rect
        return self._capture_physical(
            x=rect.x,
            y=rect.y,
            width=rect.width,
            height=rect.height,
            origin=Point(rect.x, rect.y),
            geometry_revision=0,
            binding_id=None,
            cancel=token,
            validate=lambda: self._check_display(display),
        )

    def capture_window(
        self, binding_id: str, cancel: Optional[CancelToken] = None, *, window_mode="foreground"
    ) -> Frame:
        """捕获绑定窗口的整个客户区（可见前台客户区）。

        Raises:
            CaptureError: WINDOW_NOT_FOUND（身份失效/关闭）、WINDOW_UNAVAILABLE
                （最小化/不可见）、INVALID_REGION（客户区小于 10×10）。
        """
        token: CancelToken = cancel if cancel is not None else NullCancelToken()
        token.throw_if_cancelled()
        self._check_mode(window_mode, binding_id)
        if window_mode == "background":
            return self.capture(self.run_region(None, binding_id, window_mode=window_mode), token, window_mode=window_mode)
        snapshot = self._resolve_window(binding_id)
        client = snapshot.client_rect
        if client.width < MIN_REGION_EDGE or client.height < MIN_REGION_EDGE:
            raise invalid_region("region", "窗口客户区小于 10×10 物理像素")
        return self._capture_physical(
            x=client.x,
            y=client.y,
            width=client.width,
            height=client.height,
            origin=Point(client.x, client.y),
            geometry_revision=snapshot.geometry_revision,
            binding_id=snapshot.binding_id,
            cancel=token,
            validate=lambda: self._check_window(snapshot),
        )

    # -- 图像引用 -------------------------------------------------------------

    def release_image(self, image_id: str) -> bool:
        """释放会话图像引用，返回是否（本次或之前）已释放。

        幂等、校验所有权；跨会话/过期引用 → ``IMAGE_EXPIRED``（非静默成功）。
        """
        return self._store.release(image_id)

    def image_preview(self, image_id: str, max_edge: int) -> tuple:
        """返回 ``(mime, base64)`` 预览（编码 ≤512KiB）。"""
        return self._store.preview(image_id, max_edge)

    def close(self) -> None:
        """会话收尾：释放全部引用。"""
        if self._background is not None:
            self._background.close()
        self._store.close()

    # -- 内部 -----------------------------------------------------------------

    @staticmethod
    def _validate_region_shape(region: Region) -> None:
        """空间与尺寸防御性校验（命令层已用 validate_region，此处兜底直调）。"""
        if region.space not in ("screen_physical", "window_client_physical"):
            # image_pixel 不适用于捕获：空间不匹配 → INVALID_REGION（06）
            raise invalid_region("region.space", "捕获不支持该坐标空间: %s" % region.space)
        if region.width < MIN_REGION_EDGE:
            raise invalid_region("region.width", "区域宽不得小于 %d 物理像素" % MIN_REGION_EDGE)
        if region.height < MIN_REGION_EDGE:
            raise invalid_region(
                "region.height", "区域高不得小于 %d 物理像素" % MIN_REGION_EDGE
            )

    def _check_mode(self, mode, binding):
        if mode not in ("foreground", "background") or (mode == "background" and not binding):
            raise CaptureError("INVALID_PARAMS", "后台模式需要已绑定窗口", field="window_mode")
        if mode == "background" and self._background is None:
            raise CaptureError("CAPABILITY_DENIED", "当前平台未提供后台窗口取像")

    def _resolve_window(self, binding_id: str, mode="foreground") -> WindowSnapshot:
        if not binding_id:
            raise window_not_found(binding_id)
        snapshot = self._window_provider.snapshot(binding_id)
        if snapshot.minimized or not snapshot.visible or (mode == "foreground" and not snapshot.foreground):
            raise window_unavailable(binding_id)
        return snapshot

    def _check_window(self, expected: WindowSnapshot, mode="foreground") -> None:
        current = self._resolve_window(expected.binding_id, mode)
        if mode == "background":
            current = replace(current, foreground=expected.foreground)
        if current != expected:
            raise CaptureError("STALE_FRAME", "捕获期间窗口几何已变化", retryable=True)

    def _check_display(self, expected: DisplayInfo) -> None:
        current = find_display(self.list_displays(), expected.id)
        if current is None:
            raise display_not_found(expected.id)
        if current.rect != expected.rect or current.dpi_scale != expected.dpi_scale:
            raise CaptureError("STALE_FRAME", "捕获期间显示器几何已变化", retryable=True)

    def _capture_physical(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        origin: Point,
        geometry_revision: int,
        binding_id: Optional[str],
        cancel: CancelToken,
        validate: Callable[[], None],
        read: Optional[Callable[[], RawImage]] = None,
    ) -> Frame:
        """执行物理抓取 → 登记图像 + 帧元数据 → 组装 Frame（space 恒为 screen_physical）。"""
        cancel.throw_if_cancelled()
        raw = read() if read is not None else self._capture_with_retry(x, y, width, height, cancel, validate)
        validate()
        cancel.throw_if_cancelled()
        if (raw.width, raw.height) != (width, height):
            raise capture_failed("捕获后端返回的尺寸与请求不一致", retryable=False)
        # TASK-014：登记**真实**帧位置元数据（screen_physical/真实 origin/真实
        # geometry_revision），使 image_id → Frame 可还原且与 RecognitionResult.frame_id
        # 一致。frame_id/captured_at 在同一处生成，供 FrameMeta 与 Frame 共用（值不变）。
        frame_id = self._frame_id_factory()
        captured_at = format_utc_timestamp(self._clock())
        frame_meta = FrameMeta(
            space="screen_physical",
            origin=origin,
            geometry_revision=geometry_revision,
            binding_id=binding_id,
            captured_at=captured_at,
            frame_id=frame_id,
        )
        image_ref: ImageRef = self._store.put(raw, "png", frame_meta=frame_meta)
        frame = Frame(
            frame_id=frame_id,
            image_ref=image_ref,
            space="screen_physical",
            origin=origin,
            captured_at=captured_at,
            geometry_revision=geometry_revision,
            binding_id=binding_id,
        )
        self._log(
            "capture ok frame=%s origin=(%d,%d) %dx%d gr=%d binding=%s"
            % (frame.frame_id, origin.x, origin.y, width, height,
               geometry_revision, binding_id)
        )
        return frame

    def _capture_with_retry(
        self, x: int, y: int, width: int, height: int, cancel: CancelToken,
        validate: Callable[[], None],
    ) -> RawImage:
        """仅对瞬时 CAPTURE_FAILED 自动重试一次（200ms）；其余错误直接抛出。"""
        attempts = 0
        while True:
            cancel.throw_if_cancelled()
            try:
                return self._backend.capture_bgr(x, y, width, height, cancel)
            except OSError as exc:
                # 驱动层 OSError 归一为 CAPTURE_FAILED。判据（docs/06）：
                # ① 06 错误注册表 L70 明示 CAPTURE_FAILED 的默认 retryable=true，属权威值，
                #    故此处置 retryable=True（不擅改 06 语义）；
                # ② 06 L51：「retryable 表示条件变化后允许调用方尝试，**不授权框架自动重跑动作**」。
                #    即 retryable 是给调用方（脚本/Vision/上层策略）的“可重试”提示，不等于本
                #    方法的自动重试——本服务的自动重试仅覆盖后端显式抛出的
                #    CaptureError(CAPTURE_FAILED) 瞬时失败（见下方 except 分支，至多一次、200ms）；
                #    OSError 归一后直接上抛，由上层按 MonitorConfig 策略决定是否重试。
                raise capture_failed("捕获后端 OSError：%s" % exc) from exc
            except CaptureError as exc:
                # 仅 CAPTURE_FAILED（瞬时）且 retryable 时重试一次；其它码不重试。
                if exc.code == "CAPTURE_FAILED" and exc.retryable and attempts < 1:
                    attempts += 1
                    self._retry_wait(cancel, attempts)
                    validate()
                    continue
                raise

    def _retry_wait(self, cancel: CancelToken, attempt: int) -> None:
        """一次性重试等待：等待前后检查取消（等待期间取消 → CancelledError）。"""
        cancel.throw_if_cancelled()
        self._log("CAPTURE_FAILED，第 %d 次自动重试（%.0fms）"
                  % (attempt, self._retry_delay_seconds * 1000))
        self._sleeper(self._retry_delay_seconds)
        cancel.throw_if_cancelled()
