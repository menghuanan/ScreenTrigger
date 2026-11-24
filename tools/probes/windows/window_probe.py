"""TASK-002 探针：窗口身份、客户区几何与 geometry_revision（纯 Win32）。

场景（对应任务卡 STEPS.03，TEST-006 的 OS 部分）：
1. 身份元组：hwnd/pid/进程创建时间。进程名、类名和标题供界面显示；
2. 客户区物理 rect：GetClientRect+ClientToScreen；对比 GetWindowRect 与 DWM 扩展框架边界；
3. 移动/缩放 → geometry_revision 变化、身份不变；
4. 最小化/失焦 → minimized/foreground 状态可观测（WINDOW_UNAVAILABLE 语义依据）；
5. 关闭 → 句柄失效（WINDOW_NOT_FOUND 语义），身份失效不得按标题自动重绑。

退出码：0=全过；1=有 FAIL。
用法：python tools/probes/windows/window_probe.py --json-out reports/TASK-002/window-probe-result.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from common import (GA_ROOT, HWND_TOPMOST, MiniWindow, SWP_NOMOVE,  # noqa: E402
                    SWP_NOSIZE, base_environment,
                    set_per_monitor_v2, SW_MINIMIZE, SW_RESTORE, utc_now,
                    user32, write_report)


def get_root_hwnd(hwnd) -> int:
    return user32.GetAncestor(hwnd, GA_ROOT)


def window_identity(hwnd) -> dict:
    from datetime import datetime, timedelta, timezone
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    kernel.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title_buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title_buf, 256)
    class_buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buf, 256)
    process_name = None
    started_at = None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        name_buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        try:
            times = [wt.FILETIME() for _ in range(4)]
            if kernel.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
                ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                started_at = (datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)).isoformat()
            if kernel.QueryFullProcessImageNameW(h, 0, name_buf, ctypes.byref(size)):
                process_name = name_buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel.CloseHandle(h)
    return {"hwnd": hwnd, "pid": pid.value, "process_started_at": started_at, "process_name": process_name,
            "class_name": class_buf.value, "title": title_buf.value}


def client_physical_rect(hwnd) -> dict:
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return {"x": pt.x, "y": pt.y, "w": rc.right - rc.left, "h": rc.bottom - rc.top}


def window_rects(hwnd) -> dict:
    wr = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    dwr = wt.RECT()
    dwm_ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, 9, ctypes.byref(dwr), ctypes.sizeof(dwr))
    return {
        "getwindowrect": {"x": wr.left, "y": wr.top, "w": wr.right - wr.left, "h": wr.bottom - wr.top},
        "dwm_extended_frame": ({"x": dwr.left, "y": dwr.top, "w": dwr.right - dwr.left,
                                "h": dwr.bottom - dwr.top} if dwm_ok == 0 else None),
    }


def window_states(hwnd) -> dict:
    return {
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "foreground": user32.GetForegroundWindow() == hwnd,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-002 窗口身份/几何探针")
    parser.add_argument("--json-out", default="reports/TASK-002/window-probe-result.json")
    args = parser.parse_args()

    awareness = set_per_monitor_v2()
    env = base_environment(awareness)
    scenarios = {}
    previous_geometry = None
    revision = 0

    def geometry_revision(hwnd, rect):
        nonlocal previous_geometry, revision
        user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        user32.MonitorFromWindow.restype = wt.HANDLE
        user32.GetDpiForWindow.argtypes = [wt.HWND]
        geometry = (dict(rect), user32.MonitorFromWindow(hwnd, 2), user32.GetDpiForWindow(hwnd))
        if geometry != previous_geometry:
            revision += 1
            previous_geometry = geometry
        return revision

    win = MiniWindow("ST-TASK002-WINDOW-PROBE", 100, 100, 320, 240)
    win.pump(200)
    hwnd = win.hwnd

    # 场景1：身份元组
    ident = window_identity(hwnd)
    scenarios["identity"] = {
        "expected": "hwnd>0、pid>0、进程名/类名/标题非空",
        "actual": ident,
        "pass": bool(ident["hwnd"] and ident["pid"] and ident["process_name"]
            and ident["class_name"] and ident["process_started_at"]),
    }

    # 场景2：客户区物理 rect 与 DWM 边界
    client0 = client_physical_rect(hwnd)
    rects = window_rects(hwnd)
    scenarios["client_rect"] = {
        "expected": "客户区物理 rect 有效；GetWindowRect 与 DWM 扩展边界差异如实记录",
        "actual": {"client": client0, **rects},
        "pass": client0["w"] > 0 and client0["h"] > 0,
    }

    rev0 = geometry_revision(hwnd, client0)

    # 场景3：移动 → revision 变化、身份不变
    user32.SetWindowPos(hwnd, None, 240, 200, 0, 0, SWP_NOSIZE)
    win.pump(250)
    client1 = client_physical_rect(hwnd)
    rev1 = geometry_revision(hwnd, client1)
    scenarios["move_revision"] = {
        "expected": "移动后客户区原点变化、geometry_revision 变化、身份元组不变",
        "actual": {"client_before": client0, "client_after": client1,
                   "revisions": [rev0, rev1], "revision_changed": rev0 < rev1,
                   "identity_stable": window_identity(hwnd) == ident},
        "pass": client1 != client0 and rev0 != rev1 and window_identity(hwnd) == ident,
    }

    # 场景4：缩放 → revision 再次变化
    user32.SetWindowPos(hwnd, None, 0, 0, 440, 320, SWP_NOMOVE)
    win.pump(250)
    client2 = client_physical_rect(hwnd)
    rev2 = geometry_revision(hwnd, client2)
    scenarios["resize_revision"] = {
        "expected": "缩放后客户区尺寸变化、geometry_revision 再次变化",
        "actual": {"client_after_resize": client2, "revisions": [rev1, rev2],
                   "revision_changed_again": rev1 < rev2},
        "pass": client2["w"] != client1["w"] and rev1 != rev2,
    }

    # 场景5：最小化/恢复
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    win.pump(300)
    minimized = window_states(hwnd)["minimized"]
    user32.ShowWindow(hwnd, SW_RESTORE)
    win.pump(300)
    restored = not window_states(hwnd)["minimized"]
    scenarios["minimize_restore"] = {
        "expected": "最小化状态可观测（WINDOW_UNAVAILABLE 语义），恢复后非最小化",
        "actual": {"minimized": minimized, "restored_not_minimized": restored},
        "pass": minimized and restored,
    }

    # 场景6：失焦 —— 前台切给另一窗口
    stealer = MiniWindow("ST-FOCUS-STEALER", 500, 500, 200, 120)
    stealer.pump(150)
    user32.SetForegroundWindow(stealer.hwnd)
    stealer.pump(250)
    lost = user32.GetForegroundWindow() != hwnd
    stealer.destroy()
    win.pump(100)
    scenarios["focus_loss"] = {
        "expected": "他窗抢前台后，绑定窗口 foreground==False（失焦暂停语义依据）",
        "actual": {"foreground_after_steal": lost},
        "pass": lost,
    }

    # 场景7：关闭 → 句柄失效，不按标题重绑
    hwnd_closed = hwnd
    win.destroy()
    time.sleep(0.2)
    still_valid = bool(user32.IsWindow(hwnd_closed))
    scenarios["close_identity"] = {
        "expected": "关闭后 IsWindow==False（WINDOW_NOT_FOUND 语义），身份失效不得自动重绑",
        "actual": {"hwnd": hwnd_closed, "still_valid": still_valid},
        "pass": not still_valid,
    }

    result = {
        "probe": "window_probe",
        "task": "TASK-002",
        "rebuild_command": "python tools/probes/windows/window_probe.py --json-out " + args.json_out,
        "recorded_at": utc_now(),
        "environment": env,
        "binding_design": {
            "identity_tuple": "hwnd+pid+process_started_at",
            "geometry_revision_basis": "客户区物理边界、所在显示器或窗口 DPI 改变时递增整数版本",
            "note": "重新绑定时核对进程创建时间；窗口标题只供显示。",
        },
        "scenarios": scenarios,
    }
    write_report(args.json_out, result)
    for k, v in scenarios.items():
        print(f"  {k}: {'PASS' if v['pass'] else 'FAIL'}")
    return 0 if all(v["pass"] for v in scenarios.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
