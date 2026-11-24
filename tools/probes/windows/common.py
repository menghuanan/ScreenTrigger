"""TASK-002 Windows 探针共享助手：DPI awareness、显示器拓扑、窗口工具。

仅用 Python 标准库（ctypes）。物理像素、半开矩形；虚拟桌面允许负坐标（06 契约）。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import platform
import sys
from datetime import datetime, timezone

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shcore = ctypes.windll.shcore
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

# 修正 64 位参数签名：避免 wndproc 回调中 wparam/lparam 溢出
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetForegroundWindow.argtypes = []
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.POINT)]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetAncestor.argtypes = [ctypes.c_void_p, wt.UINT]
user32.GetAncestor.restype = ctypes.c_void_p
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [ctypes.c_void_p, wt.LPWSTR, ctypes.c_int]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p
shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]

import time  # noqa: E402  （MiniWindow.pump 需要）

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
MDT_EFFECTIVE_DPI = 0
DWMWA_EXTENDED_FRAME_BOUNDS = 9
GA_ROOT = 2
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK_CONTROL, VK_SHIFT, VK_MENU = 0x11, 0x10, 0x12
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_per_monitor_v2() -> int:
    """进程级 Per-Monitor V2；返回 awareness 状态（2=per-monitor aware）。"""
    try:
        user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except Exception:
        pass  # 较旧系统无此 API，回落 SetProcessDPIAware
    if hasattr(user32, "GetAwarenessFromDpiAwarenessContext"):
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        ctx = user32.GetThreadDpiAwarenessContext()
        return user32.GetAwarenessFromDpiAwarenessContext(ctx)
    return int(bool(user32.IsProcessDPIAware()))


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32),
    ]


def enumerate_monitors() -> list[dict]:
    monitors_raw: list[MONITORINFOEXW] = []

    def on_monitor(hmonitor, hdc, lprect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
            monitors_raw.append(mi)
        return True

    cb = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.RECT), wt.LPARAM
    )(on_monitor)
    if not user32.EnumDisplayMonitors(None, None, cb, 0):
        pass  # 回调异常已在 stderr 记录；monitors_raw 保持实际结果
    out = []
    for mi in monitors_raw:
        dpi_x = ctypes.c_uint(96)
        dpi_y = ctypes.c_uint(96)
        hmon = user32.MonitorFromPoint(wt.POINT(0, 0), 1)  # 默认占位，逐个枚举时重新取
        # EnumDisplayMonitors 回调句柄未透出，改由 GetMonitorInfo 遍历：
        # 逐显示器 DPI 用 MonitorFromPoint(矩形中心)
        cx = (mi.rcMonitor.left + mi.rcMonitor.right) // 2
        cy = (mi.rcMonitor.top + mi.rcMonitor.bottom) // 2
        hmon = user32.MonitorFromPoint(wt.POINT(cx, cy), 1)  # MONITOR_DEFAULTTONEAREST
        try:
            shcore.GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI,
                                    ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        except Exception:
            dpi_x = ctypes.c_uint(user32.GetDpiForSystem())
        r = mi.rcMonitor
        out.append({
            "device": mi.szDevice,
            "physical_rect": {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top},
            "dpi": int(dpi_x.value),
            "dpi_scale": round(dpi_x.value / 96, 4),
            "primary": bool(mi.dwFlags & 1),
        })
    return out


def virtual_screen() -> dict:
    return {
        "x": user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        "y": user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        "w": user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        "h": user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    }


def topology_revision(monitors: list[dict]) -> str:
    blob = json_dumps(sorted(
        (m["device"], m["physical_rect"]["x"], m["physical_rect"]["y"],
         m["physical_rect"]["w"], m["physical_rect"]["h"], m["dpi"]) for m in monitors
    ))
    return hashlib.sha256(blob).hexdigest()[:16]


def json_dumps(obj) -> bytes:
    import json
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")


def base_environment(awareness: int) -> dict:
    monitors = enumerate_monitors()
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    per_monitor_v2 = user32.AreDpiAwarenessContextsEqual(
        user32.GetThreadDpiAwarenessContext(), DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    return {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": sys.version.replace("\n", " "),
        "dpi_awareness_status": awareness,  # 2 = PER_MONITOR_AWARE
        "per_monitor_v2": bool(per_monitor_v2),
        "monitors": monitors,
        "virtual_screen": virtual_screen(),
        "topology_revision": topology_revision(monitors),
        "recorded_at": utc_now(),
    }


def write_report(path: str, result: dict) -> str:
    import json
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# ---------------- 最小 Win32 窗口框架（无 tkinter 依赖） ----------------

WM_MOUSEMOVE = 0x0200
WM_DESTROY = 0x0002
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
SW_HIDE, SW_SHOW, SW_MINIMIZE, SW_RESTORE = 0, 5, 6, 9
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t
)


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", wt.UINT),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", wt.DWORD), ("pt", wt.POINT)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class MiniWindow:
    """探针专用顶层窗口：记录自身收到的 WM_MOUSEMOVE；消息在同线程泵送。"""

    _registry: dict[int, "MiniWindow"] = {}

    def __init__(self, title: str, x: int, y: int, w: int, h: int, bg_brush=None,
                 popup=False):
        self.motions: list[tuple[int, int]] = []
        self.hwnd = None
        self._proc_ref = WNDPROC(self._wndproc)
        style = (WS_POPUP if popup else WS_OVERLAPPEDWINDOW) | WS_VISIBLE
        hInst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc_ref
        wc.hInstance = hInst
        wc.lpszClassName = f"STProbe{abs(hash(title)) & 0xFFFF:04X}"
        wc.hbrBackground = bg_brush  # None → 系统默认
        atom = user32.RegisterClassW(ctypes.byref(wc))
        self.class_name = wc.lpszClassName
        self.hInstance = hInst
        self._atom = atom
        self.hwnd = user32.CreateWindowExW(
            0, wc.lpszClassName, title, style, x, y, w, h,
            None, None, hInst, None)
        MiniWindow._registry[ctypes.c_void_p(self.hwnd).value or 0] = self

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_MOUSEMOVE:
            self.motions.append((ctypes.c_short(lparam & 0xFFFF).value,
                                 ctypes.c_short((lparam >> 16) & 0xFFFF).value))
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump(self, ms: int = 100):
        end = time.perf_counter() + ms / 1000
        msg = MSG()
        while time.perf_counter() < end:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)

    def destroy(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            user32.DestroyWindow(self.hwnd)
        user32.UnregisterClassW(self.class_name, self.hInstance)
        MiniWindow._registry.pop(ctypes.c_void_p(self.hwnd).value or 0, None)
        self.hwnd = None

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass


def create_edit_child(parent_hwnd, text: str, x, y, w, h) -> int:
    return user32.CreateWindowExW(
        0, "EDIT", text, WS_CHILD | WS_VISIBLE | 0x00800000,  # WS_BORDER
        x, y, w, h, parent_hwnd, None, kernel32.GetModuleHandleW(None), None)


def edit_get_text(edit_hwnd) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(edit_hwnd, buf, 1024)
    return buf.value


def edit_set_text(edit_hwnd, text: str):
    user32.SetWindowTextW(edit_hwnd, text)


def client_screen_origin_helper(hwnd) -> tuple[int, int]:
    """客户区左上角的屏幕物理坐标。"""
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y
