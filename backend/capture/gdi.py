# -*- coding: utf-8 -*-
"""Windows GDI/BitBlt 捕获后端与显示器枚举（TASK-007，TASK-002 已验证路径）。

复刻 TASK-002 探针（tools/probes/windows/{common,capture_probe}.py）已验证的
Win32/DPI/BitBlt 手法，但**不 import 探针**（探针是独立证据工具）：

- 进程启用 Per-Monitor V2 DPI awareness（07 §1：创建窗口前启用），坐标统一物理像素；
- ``GdiDisplayEnumerator``：EnumDisplayMonitors + GetMonitorInfo + GetDpiForMonitor，
  产出 02 的 ``DisplayInfo``（id=设备名、物理 rect、dpi_scale、primary）；
- ``GdiCaptureBackend``：GetDC(NULL)+CreateCompatibleBitmap+BitBlt(SRCCOPY|CAPTUREBLT)
  +GetDIBits（32bpp top-down）→ 去 Alpha 得 BGR uint8。``use_captureblt=False``
  （TASK-008）改用纯 ``SRCCOPY``，使 WS_EX_LAYERED 覆盖层不被捕获。

仅标准库（ctypes）；本模块只在 Windows 可用，由 ``build_default_capture_service``
延迟 import（非 Windows 或缺 API 时捕获能力保持 unavailable）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from typing import List

from ..types.region import DisplayInfo, Rect

from .cancel import CancelToken
from .errors import capture_failed
from .ports import RawImage

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# ---- 常量 -----------------------------------------------------------------

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
MDT_EFFECTIVE_DPI = 0
MONITOR_DEFAULTTONEAREST = 2
PRIMARY_MONITOR_FLAG = 1
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0

# ---- 函数签名（64 位句柄按 c_void_p 处理，避免截断） -----------------------

_user32.GetDC.argtypes = [ctypes.c_void_p]
_user32.GetDC.restype = ctypes.c_void_p
_user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
_user32.MonitorFromPoint.restype = ctypes.c_void_p
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int

_gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
_gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
_gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint]
_gdi32.BitBlt.restype = ctypes.c_int
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteObject.restype = ctypes.c_int
_gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
_gdi32.GetDIBits.restype = ctypes.c_int


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wt.DWORD * 3)]


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


def ensure_per_monitor_v2() -> bool:
    """进程级 Per-Monitor V2 DPI awareness（07 §1）。

    Returns:
        True 表示设置成功（较旧系统回落 SetProcessDPIAware）；失败返回 False。
    """
    try:
        if _user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(_user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


class GdiDisplayEnumerator:
    """EnumDisplayMonitors 驱动的显示器枚举（物理像素）。"""

    def list_displays(self) -> List[DisplayInfo]:
        monitors: List[_MonitorInfoEx] = []

        def _callback(hmonitor, _hdc, _lprect, _lparam):
            info = _MonitorInfoEx()
            info.cbSize = ctypes.sizeof(_MonitorInfoEx)
            if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                monitors.append(info)
            return True

        callback = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wt.RECT), wt.LPARAM,
        )(_callback)
        if not _user32.EnumDisplayMonitors(None, None, callback, 0):
            raise capture_failed("EnumDisplayMonitors 失败", retryable=False)

        displays: List[DisplayInfo] = []
        for info in monitors:
            rect = info.rcMonitor
            center = wt.POINT(
                (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            )
            hmon = _user32.MonitorFromPoint(center, MONITOR_DEFAULTTONEAREST)
            dpi = self._effective_dpi(hmon)
            displays.append(
                DisplayInfo(
                    id=info.szDevice,
                    rect=Rect(
                        x=rect.left,
                        y=rect.top,
                        width=rect.right - rect.left,
                        height=rect.bottom - rect.top,
                    ),
                    dpi_scale=round(dpi / 96.0, 4),
                    primary=bool(info.dwFlags & PRIMARY_MONITOR_FLAG),
                )
            )
        return displays

    @staticmethod
    def _effective_dpi(hmonitor: ctypes.c_void_p) -> int:
        """逐显示器 effective DPI；shcore 不可用时回落系统 DPI。"""
        dpi_x = ctypes.c_uint(96)
        dpi_y = ctypes.c_uint(96)
        try:
            _shcore = ctypes.windll.shcore
            _shcore.GetDpiForMonitor.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            ]
            hr = _shcore.GetDpiForMonitor(
                hmonitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
            )
            if hr == 0 and dpi_x.value > 0:
                return int(dpi_x.value)
        except (AttributeError, OSError):
            pass
        try:
            return int(_user32.GetDpiForSystem())
        except (AttributeError, OSError):
            return 96


class GdiCaptureBackend:
    """BitBlt 捕获后端：屏幕物理矩形 → BGR uint8 帧。

    ``use_captureblt``（TASK-008）：默认 ``True`` 保留 TASK-007 既有行为
    （``SRCCOPY | CAPTUREBLT``，可捕获分层窗口）。置 ``False`` 时只用 ``SRCCOPY``
    ——**分层窗口（WS_EX_LAYERED）不会被捕获**，用于框选覆盖层「覆盖层可见时
    仍采到干净桌面、不递归截到覆盖层」（07 §1 / F01）。开关不改变既有语义：
    默认值与 TASK-007 完全一致，仅新增可选行为。
    """

    name = "gdi-bitblt"

    def __init__(self, use_captureblt: bool = True) -> None:
        self.use_captureblt = bool(use_captureblt)

    def _blt_rop(self) -> int:
        """BitBlt 光栅操作码：按开关决定是否带 CAPTUREBLT（分层窗口捕获位）。"""
        return SRCCOPY | (CAPTUREBLT if self.use_captureblt else 0)

    def capture_bgr(
        self, x: int, y: int, width: int, height: int, cancel: CancelToken
    ) -> RawImage:
        """抓取物理矩形 ``[x,x+w)×[y,y+h)``（支持负坐标虚拟桌面）。"""
        cancel.throw_if_cancelled()
        if width <= 0 or height <= 0:
            raise capture_failed("捕获尺寸必须为正", retryable=False)

        hdc_screen = _user32.GetDC(None)
        if not hdc_screen:
            raise capture_failed("GetDC 失败")
        hdc_mem = _gdi32.CreateCompatibleDC(hdc_screen)
        hbitmap = _gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not hdc_mem or not hbitmap:
            if hbitmap:
                _gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(None, hdc_screen)
            raise capture_failed("创建兼容 DC/位图失败")

        previous = None
        try:
            previous = _gdi32.SelectObject(hdc_mem, hbitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                previous = None
                raise capture_failed("无法选入捕获位图")
            if not _gdi32.BitBlt(
                hdc_mem, 0, 0, width, height,
                hdc_screen, x, y, self._blt_rop(),
            ):
                raise capture_failed("BitBlt 失败（可能为受保护内容或显示器变化）")

            if not _gdi32.SelectObject(hdc_mem, previous):
                raise capture_failed("无法释放捕获位图以读取像素")
            previous = None
            header = _BitmapInfo()
            header.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            header.bmiHeader.biWidth = width
            header.bmiHeader.biHeight = -height  # 负值 → 自上而下
            header.bmiHeader.biPlanes = 1
            header.bmiHeader.biBitCount = 32
            header.bmiHeader.biCompression = BI_RGB

            buffer = ctypes.create_string_buffer(width * height * 4)
            scanned = _gdi32.GetDIBits(
                hdc_mem, hbitmap, 0, height, buffer, ctypes.byref(header), DIB_RGB_COLORS
            )
            if scanned != height:
                raise capture_failed("GetDIBits 行数不完整")

            cancel.throw_if_cancelled()
            raw = bytes(buffer)  # BGRA，长度 w*h*4
            bgr = bytearray(width * height * 3)
            bgr[0::3] = raw[0::4]
            bgr[1::3] = raw[1::4]
            bgr[2::3] = raw[2::4]
            return RawImage(width=width, height=height, data=bytes(bgr))
        finally:
            if previous:
                _gdi32.SelectObject(hdc_mem, previous)
            _gdi32.DeleteObject(hbitmap)
            _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(None, hdc_screen)
