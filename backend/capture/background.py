"""Bounded client-area painting for an explicitly bound, non-minimized Win32 window."""
import ctypes
from ctypes import wintypes as wt
import multiprocessing
import threading
import time
from backend.worker_process import WorkerProcess

from .errors import CaptureError
from .ports import RawImage

MAX_PIXELS = 16_777_216
PAINT_TIMEOUT_SECONDS = 2.0
MAX_CONCURRENT_PAINTS = 3


def _coverage(user, gdi, hwnd, client):
    """Classify only an ordinary rectangular foreground client covering the whole target."""
    user.GetForegroundWindow.restype = wt.HWND
    other = user.GetForegroundWindow()
    if not other or other == hwnd:
        return "foreground"
    if user.GetWindowLongW(other, -20) & (0x00080000 | 0x20):
        return "non_foreground"
    gdi.CreateRectRgn.argtypes = [ctypes.c_int] * 4
    gdi.CreateRectRgn.restype = wt.HRGN
    user.GetWindowRgn.argtypes = [wt.HWND, wt.HRGN]
    region = gdi.CreateRectRgn(0,0,0,0)
    if not region:
        return "non_foreground"
    try:
        if user.GetWindowRgn(other, region):
            return "non_foreground"
    finally:
        gdi.DeleteObject(region)
    outer, origin, target = wt.RECT(), wt.POINT(), wt.POINT()
    user.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    if not (user.GetClientRect(other,ctypes.byref(outer)) and user.ClientToScreen(other,ctypes.byref(origin))
            and user.ClientToScreen(hwnd,ctypes.byref(target))):
        return "non_foreground"
    if (origin.x <= target.x and origin.y <= target.y
            and origin.x+outer.right >= target.x+client.right and origin.y+outer.bottom >= target.y+client.bottom):
        return "fully_occluded"
    return "non_foreground"


def _paint(connection, pixels, hwnd, pid, client_size, crop):
    # The owning process can block indefinitely inside PrintWindow. Only this child calls it.
    from .gdi import (_BitmapInfo, _BitmapInfoHeader, _gdi32, _user32, ensure_per_monitor_v2)
    ensure_per_monitor_v2()
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _user32.IsIconic.argtypes = [wt.HWND]
    _user32.IsWindowVisible.argtypes = [wt.HWND]
    _user32.GetWindowDisplayAffinity.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    _user32.SendMessageW.restype = wt.LPARAM
    _user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    _gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(_BitmapInfo), wt.UINT,
        ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    _gdi32.CreateDIBSection.restype = wt.HBITMAP
    dc = bitmap = previous = None
    try:
        owner, size, affinity = wt.DWORD(), wt.RECT(), wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid or not _user32.GetClientRect(hwnd, ctypes.byref(size)):
            raise CaptureError("WINDOW_NOT_FOUND", "绑定目标已关闭或改变")
        width, height = client_size
        if (size.right, size.bottom) != (width, height):
            raise CaptureError("STALE_FRAME", "后台绘制前客户区尺寸已变化")
        if _user32.IsIconic(hwnd) or not _user32.IsWindowVisible(hwnd):
            raise CaptureError("WINDOW_UNAVAILABLE", "后台绘制要求可见且已还原的客户区")
        affinity_known = _user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        layered = _user32.GetWindowLongW(hwnd, -20) & 0x00080000
        if affinity.value or (layered and not affinity_known):
            raise CaptureError("CAPABILITY_DENIED", "窗口设置了内容捕获保护")
        # A compositor can still serve the last surface of a hung UI. Require the
        # bound window to service a side-effect-free message for this sample.
        _user32.SendMessageW(hwnd, 0, 0, 0)
        dc = _gdi32.CreateCompatibleDC(None)
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
        info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
        address = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(address), None, 0) if dc else None
        if not bitmap or not address.value:
            raise CaptureError("CAPTURE_FAILED", "无法分配后台客户区位图")
        previous = _gdi32.SelectObject(dc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            raise CaptureError("CAPTURE_FAILED", "无法选择后台客户区位图")
        # Painting is window-owned. An untouched marker guards against accepting unsupported renderers.
        ctypes.memset(address, 0xa5, width * height * 4)
        if not _user32.PrintWindow(hwnd, dc, 1):
            raise CaptureError("CAPABILITY_DENIED", "该窗口未能绘制后台客户区")
        _gdi32.GdiFlush()
        x, y, w, h = crop
        rows = b"".join(ctypes.string_at(address.value+4*((y+dy)*width+x),4*w) for dy in range(h))
        if rows == bytes([0xa5])*len(rows):
            raise CaptureError("CAPABILITY_DENIED", "目标没有绘制请求区域，后台取像不可用")
        dest = memoryview(pixels).cast("B")
        dest[0::3], dest[1::3], dest[2::3] = rows[0::4], rows[1::4], rows[2::4]
        connection.send((None, _coverage(_user32, _gdi32, hwnd, size)))
    except Exception as exc:
        connection.send((getattr(exc,"code","CAPTURE_FAILED"), str(exc)))
    finally:
        if previous:
            _gdi32.SelectObject(dc, previous)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if dc:
            _gdi32.DeleteDC(dc)
        connection.close()


class WindowPaintBackend:
    def __init__(self):
        self.closed = threading.Event()
        self.close_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT_PAINTS)
        self.lock = threading.Lock()
        self.binding = None
        self.observations = {}
        self.reason = None

    def capabilities(self, binding):
        with self.lock:
            seen = self.observations if self.binding == binding else {}
            return {key: seen.get(key,"unknown") for key in ("non_foreground","fully_occluded")}

    def unavailable_reason(self, binding):
        with self.lock:
            return self.reason if self.binding == binding else None

    def _observe(self, binding, values, reason=None):
        with self.lock:
            if self.binding != binding:
                self.observations = {}
                self.binding = binding
            self.reason = reason
            self.observations.update(values)

    def read(self, snapshot, x, y, width, height, cancel):
        client = snapshot.client_rect
        if snapshot.handle is None or snapshot.pid is None:
            raise CaptureError("CAPABILITY_DENIED", "缺少已验证的后台窗口身份")
        if not (0 < client.width*client.height <= MAX_PIXELS and 0 <= x < x+width <= client.width
                and 0 <= y < y+height <= client.height):
            raise CaptureError("INVALID_REGION", "客户区或后台采样区域超过允许范围")
        if not self.slots.acquire(blocking=False):
            raise CaptureError("BUSY", "后台窗口绘制作业已达上限", retryable=True)
        try:
            ctx = multiprocessing.get_context("spawn")
            pixels = ctx.RawArray("B", width*height*3)
            reader, writer = ctx.Pipe(duplex=False)
            process = WorkerProcess(target=_paint, args=(writer,pixels,snapshot.handle,snapshot.pid,
                (client.width,client.height),(x,y,width,height)), daemon=True)
            try:
                cancel.throw_if_cancelled()
                if self.closed.is_set():
                    raise CaptureError("INVALID_STATE", "捕获会话已关闭")
                process.start()
                writer.close()
                expires = time.monotonic() + PAINT_TIMEOUT_SECONDS
                while not reader.poll(.015):
                    cancel.throw_if_cancelled()
                    if self.closed.is_set():
                        raise CaptureError("INVALID_STATE", "捕获会话已关闭")
                    if not process.is_alive():
                        raise CaptureError("CAPTURE_FAILED", "后台绘制进程提前退出")
                    if time.monotonic() >= expires:
                        raise CaptureError("CAPTURE_FAILED", "目标窗口绘制超过 2 秒，已回收捕获进程", retryable=True)
                code, detail = reader.recv()
                if code:
                    if code == "CAPABILITY_DENIED":
                        self._observe(snapshot.binding_id, {"non_foreground":"unavailable","fully_occluded":"unavailable"}, detail)
                    raise CaptureError(code, detail)
                cancel.throw_if_cancelled()
                if self.closed.is_set():
                    raise CaptureError("INVALID_STATE", "后台绘制结果属于已关闭会话")
                if detail != "foreground":
                    self._observe(snapshot.binding_id,{"non_foreground":"available"})
                if detail == "fully_occluded":
                    self._observe(snapshot.binding_id,{"fully_occluded":"available"})
                return RawImage(width,height,bytes(pixels))
            except (EOFError,OSError) as exc:
                raise CaptureError("CAPTURE_FAILED", "后台绘制进程中止") from exc
            finally:
                if process.pid is not None:
                    process.join(.05)
                    if process.is_alive():
                        process.kill()
                        process.join()
                process.close()
                reader.close()
                writer.close()
        finally:
            self.slots.release()

    def close(self):
        self.closed.set()
        with self.close_lock:
            for _ in range(MAX_CONCURRENT_PAINTS):
                self.slots.acquire()
            for _ in range(MAX_CONCURRENT_PAINTS):
                self.slots.release()
