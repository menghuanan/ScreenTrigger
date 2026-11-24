"""TASK-002 探针：实时干净捕获（BitBlt 路径）与覆盖层隐藏排除。

场景（对应任务卡 STEPS.02）：
1. 实时性：专用 Win32 测试窗口内容（EDIT 计数文本）持续变化，连续捕获产生不同帧；
2. 像素误差：已知物理坐标的纯色标记窗在捕获帧中的 bbox 偏差（目标≤1 物理像素）；
3. 覆盖层排除：测试窗 SW_HIDE 后立即捕获，确认窗口像素消失（主窗隐藏后才捕获的语义），再恢复；
4. 时延：BitBlt 单帧捕获耗时 p50/p95（预览端到端 p95≤200ms 属 TEST-015 整机门槛，不在此冒充）；
5. 后端限制：Windows Graphics Capture 属 Rust Host 评估项，本探针仅覆盖 GDI/BitBlt 路径。

退出码：0=全过；1=有 FAIL；2=环境不可测。
用法：python tools/probes/windows/capture_probe.py --json-out reports/TASK-002/capture-probe-result.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time
sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
from common import (MiniWindow, base_environment, create_edit_child,  # noqa: E402
                    edit_get_text, edit_set_text, gdi32, set_per_monitor_v2,
                    SW_HIDE, SW_SHOW, utc_now, user32, write_report)

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None


def find_red_bbox(img):
    px = img.load()
    w, h = img.size
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            if r > 200 and g < 60 and b < 60:
                minx = min(minx, x)
                maxx = max(maxx, x)
                miny = min(miny, y)
                maxy = max(maxy, y)
    if maxx < 0:
        return None
    return (minx, miny, maxx + 1, maxy + 1)


def client_screen_origin(hwnd) -> tuple[int, int]:
    import ctypes.wintypes as wt
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def main() -> int:
    parser = argparse.ArgumentParser(description="TASK-002 实时捕获探针")
    parser.add_argument("--json-out", default="reports/TASK-002/capture-probe-result.json")
    args = parser.parse_args()

    awareness = set_per_monitor_v2()
    env = base_environment(awareness)
    scenarios = {}

    if ImageGrab is None:
        scenarios["pillow_available"] = {"expected": True, "actual": False, "pass": False,
                                         "note": "pillow 未安装，BitBlt 路径不可测"}
        result = {"probe": "capture_probe", "task": "TASK-002", "recorded_at": utc_now(),
                  "environment": env, "scenarios": scenarios}
        write_report(args.json_out, result)
        print("[capture_probe] pillow unavailable")
        return 2

    # 专用测试窗口：EDIT 计数文本每 tick 更新
    win = MiniWindow("ST-TASK002-CAPTURE-PROBE", 80, 80, 300, 200)
    edit = create_edit_child(win.hwnd, "FRAME 000000", 10, 10, 270, 40)
    win.pump(200)
    ox, oy = client_screen_origin(win.hwnd)
    region = (ox + 5, oy + 5, ox + 295, oy + 195)

    # 场景1：实时性
    digests = set()
    for i in range(8):
        edit_set_text(edit, f"FRAME {i:06d}")
        win.pump(60)
        img = ImageGrab.grab(bbox=region, all_screens=True)
        digests.add(img.tobytes())
    scenarios["realtime_frames"] = {
        "expected": "8 轮内容更新捕获 ≥4 种不同帧（非静态截图）",
        "actual": {"captures": 8, "distinct": len(digests),
                   "edit_text_last": edit_get_text(edit)},
        "pass": len(digests) >= 4,
    }

    # 场景2：像素误差 —— 纯色标记窗，客户区即整窗（popup 无边框）
    red_brush = gdi32.CreateSolidBrush(0x0000FF)  # COLORREF 0x00BBGGRR → 红色
    marker = MiniWindow("ST-MARKER", 420, 420, 100, 80, bg_brush=red_brush, popup=True)
    marker.pump(250)
    shot = ImageGrab.grab(bbox=(420, 420, 520, 500), all_screens=True)
    bbox = find_red_bbox(shot)
    expected = (0, 0, 100, 80)  # 相对裁剪区域：popup 客户区=整窗
    err = None
    if bbox:
        err = max(abs(a - b) for a, b in zip(bbox, expected))
    scenarios["pixel_error"] = {
        "expected": "标记窗 bbox 相对裁剪区偏差 ≤1 物理像素",
        "actual": {"marker_rect": (420, 420, 520, 500), "measured_bbox_rel": bbox,
                   "expected_rel": expected, "max_error_px": err},
        "pass": bbox is not None and err is not None and err <= 1,
    }

    # 场景3：时延
    latencies = []
    for _ in range(30):
        t0 = time.perf_counter()
        ImageGrab.grab(bbox=region, all_screens=True)
        latencies.append((time.perf_counter() - t0) * 1000)
        win.pump(15)
    lat_sorted = sorted(latencies)
    scenarios["capture_latency_ms"] = {
        "expected": "BitBlt 单帧捕获耗时分布（预览端到端 p95≤200ms 属 TEST-015，不在此冒充）",
        "actual": {"p50": round(lat_sorted[len(lat_sorted) // 2], 1),
                   "p95": round(lat_sorted[max(0, int(len(lat_sorted) * 0.95) - 1)], 1),
                   "max": round(lat_sorted[-1], 1), "n": len(latencies)},
        "pass": True,
    }

    # 场景4：隐藏排除 + 恢复
    marker.pump(150)
    before = ImageGrab.grab(bbox=(420, 420, 520, 500), all_screens=True).tobytes()
    user32.ShowWindow(marker.hwnd, SW_HIDE)
    marker.pump(250)  # 隐藏完成后才捕获
    after = ImageGrab.grab(bbox=(420, 420, 520, 500), all_screens=True).tobytes()
    user32.ShowWindow(marker.hwnd, SW_SHOW)
    marker.pump(150)
    restored_visible = bool(user32.IsWindowVisible(marker.hwnd))
    marker.destroy()
    gdi32.DeleteObject(red_brush)
    scenarios["hide_restore"] = {
        "expected": "隐藏后该区域帧变化（标记窗像素消失），SW_SHOW 后窗口重新可见",
        "actual": {"frames_differ": before != after, "restored_visible": restored_visible},
        "pass": before != after and restored_visible,
    }

    win.destroy()
    result = {
        "probe": "capture_probe",
        "task": "TASK-002",
        "rebuild_command": "python tools/probes/windows/capture_probe.py --json-out " + args.json_out,
        "recorded_at": utc_now(),
        "environment": env,
        "backend_note": "GDI/BitBlt(ImageGrab) 路径；WGC 捕获+窗口排除能力由 Rust Host 评估（07§1），属 VERIFY.02 升级决策",
        "scenarios": scenarios,
    }
    write_report(args.json_out, result)
    for k, v in scenarios.items():
        print(f"  {k}: {'PASS' if v['pass'] else 'FAIL'} -> {str(v['actual'])[:150]}")
    return 0 if all(v["pass"] for v in scenarios.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
