"""Exercise the real WinUI probe on a dedicated changing target. Run from the repo root."""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image
from common import (GA_ROOT, MiniWindow, base_environment,
                    gdi32, set_per_monitor_v2, user32)
from input_probe import INPUT, MOUSEINPUT, vk_event


user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.TextOutW.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.LPCWSTR, ctypes.c_int]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]


def qpc():
    value = ctypes.c_longlong()
    ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(value))
    return value.value


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def invoke(hwnd, automation_id):
    # UIA targets a control within this HWND even when another application is running.
    script = f"""
Add-Type -AssemblyName UIAutomationClient
$root = [Windows.Automation.AutomationElement]::FromHandle([intptr]{int(hwnd)})
$cond = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::AutomationIdProperty, '{automation_id}')
$button = $root.FindFirst([Windows.Automation.TreeScope]::Descendants, $cond)
if (!$button -or !$button.Current.IsEnabled) {{ exit 2 }}
$button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
"""
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)


class TimerTarget(MiniWindow):
    """Each paint has a unique RGB identifier and reference edges in physical pixels."""

    def __init__(self, monitor):
        self.frames = []
        self.last_paint = 0.0
        self.region = (monitor["x"] + 75, monitor["y"] + 100, 160, 96)
        super().__init__("ScreenTrigger TASK-002 timer", *self.region, popup=True)
        require(self.hwnd, "timer HWND unavailable")
        user32.SetWindowPos(self.hwnd, -1, *self.region, 0x10)  # TOPMOST, NOACTIVATE
        self.paint()

    def paint(self):
        seq = len(self.frames)
        color = (30 + seq % 160, 130 + (seq // 160) % 60, 89)
        dc = user32.GetDC(self.hwnd)
        require(dc, "timer DC unavailable")
        started = qpc()
        try:
            for rect, rgb in [((0, 0, 160, 96), (20, 47, 70)), ((2, 2, 158, 94), color)]:
                brush = gdi32.CreateSolidBrush(rgb[0] | rgb[1] << 8 | rgb[2] << 16)
                try:
                    require(user32.FillRect(dc, ctypes.byref(wt.RECT(*rect)), brush), "timer paint failed")
                finally:
                    gdi32.DeleteObject(brush)
            label = f"FRAME {seq:04d}"
            gdi32.SetBkMode(dc, 1)
            gdi32.TextOutW(dc, 8, 6, label, len(label))
            gdi32.GdiFlush()
        finally:
            user32.ReleaseDC(self.hwnd, dc)
        self.frames.append(dict(seq=seq, rgb=color, started_ticks=started))
        self.last_paint = time.monotonic()

    def wait(self, predicate, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(10)
            if time.monotonic() - self.last_paint >= .22:
                self.paint()
            result = predicate()
            if result:
                return result
        raise TimeoutError("timed out waiting for the probe")


def own_window(pid, title):
    hwnd = user32.FindWindowW(None, title)
    actual = wt.DWORD()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(actual))
    return hwnd if actual.value == pid else None


def send(items):
    require(user32.SendInput(len(items), (INPUT * len(items))(*items), ctypes.sizeof(INPUT)) == len(items),
            "SendInput did not dispatch the requested batch")


def drag(hwnd, a, b, target):
    def move(point):
        hit = user32.WindowFromPoint(wt.POINT(*point))
        require(user32.GetForegroundWindow() == hwnd and user32.GetAncestor(hit, GA_ROOT) == hwnd,
                "overlay does not own the foreground and target point")
        require(user32.SetCursorPos(*point), "cannot move to selection point")
        target.pump(45)

    require(not user32.GetAsyncKeyState(1) & 0x8000, "left mouse button already held")
    move(a)
    down, up = INPUT(type=0), INPUT(type=0)
    down.mi, up.mi = MOUSEINPUT(dwFlags=2), MOUSEINPUT(dwFlags=4)
    send([down])
    try:
        target.pump(60)
        move(b)
    finally:
        send([up])
    target.pump(80)


def read_events(folder):
    path = folder / "events.jsonl"
    if not path.exists():
        return []
    # The writer may be partway through the last line.
    return [json.loads(line) for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n")]


def exercise(exe, out, case, monitor, target, composed=False):
    folder = out / case
    folder.mkdir()
    flags = {"timer": ["--measure-preview"], "retry": ["--fail-once"], "failure": ["--fail-capture"]}
    with (folder / "stderr.log").open("wb") as stderr:
        args = [str(exe), "--output", str(folder), *flags.get(case, [])]
        if composed:
            args.append("--composed-capture")
        app = subprocess.Popen(args, stderr=stderr)
        try:
            def find_main():
                require(app.poll() is None, f"probe exited: {app.returncode}")
                return own_window(app.pid, "ScreenTrigger · 框选探针")

            main = target.wait(find_main, 12)
            user32.SetWindowPos(main, 0, monitor["x"] + 40, monitor["y"] + 55, 0, 0, 1)
            user32.SetForegroundWindow(main)
            invoke(main, "StartSelection")
            session = target.wait(lambda: next((p for p in folder.iterdir() if p.is_dir()), None))
            def events(kind):
                return [e["data"] for e in read_events(session) if e["kind"] == kind]
            target.wait(lambda: events("overlay_ready") or events("error"))
            require(not events("error"), str(events("error")))
            overlay = own_window(app.pid, "ScreenTrigger · 选择区域")
            require(overlay and not user32.IsWindowVisible(main), "main/overlay visibility mismatch")
            x, y, w, h = target.region
            if case == "escape":
                require(user32.GetForegroundWindow() == overlay, "Esc target lost foreground")
                send([vk_event(27), vk_event(27, up=True)])
            elif case == "close":
                user32.PostMessageW(overlay, 0x10, 0, 0)
            else:
                if case == "minimum":
                    drag(overlay, (x + 25, y + 29), (x + 34, y + 39), target)
                    target.wait(lambda: events("pointer_released"))
                    rejected = events("pointer_released")[-1]
                    require(not rejected["valid"] and rejected["region"]["width"] == 9, str(rejected))
                    x, y, w, h = x + 25, y + 29, 10, 10
                first, last = (x, y), (x + w, y + h)
                if case == "reverse":
                    first, last = last, first
                drag(overlay, first, last, target)
                if case != "failure":
                    target.wait(lambda: any(e["region"] == dict(x=x, y=y, width=w, height=h)
                                            for e in events("preview_updated")))
                    if case == "timer":
                        target.wait(lambda: len({(e["red"],e["green"],e["blue"]) for e in events("preview_rendered")})
                                    >= 20, 14)
                    invoke(overlay, "ConfirmSelection")
            result = target.wait(lambda: events("result") or events("error"))
            expected = {"failure": "capture_failed", "close": "window_closed", "escape": "escape"}.get(case, "confirmed")
            require(result[-1].get("outcome") == expected, str(result))
            restored = events("restored")[-1]
            require(restored["visible"] and restored["foreground"] == main and
                    restored["focus"] != "Unfocused" and user32.IsWindowVisible(main), str(restored))
            metrics = dict(outcome=expected, session=session.name)
            if case in ("failure", "retry"):
                require(len(events("capture_retry")) == 1, "capture retry count differs from one")
            if expected != "confirmed":
                require(not list(session.glob("*.png")), "unexpected image after cancellation/failure")
                return metrics
            required = dict(x=x, y=y, width=w, height=h)
            require(result[-1]["region"] == required, str(result))
            palette = {tuple(frame["rgb"]): frame["started_ticks"] for frame in target.frames}
            for name in ("preview", "final"):
                with Image.open(session / (name + ".png")) as image:
                    rgb = image.convert("RGB")
                    require(rgb.size == (w, h) and rgb.getpixel((w//2, h//2)) in palette, f"{name} misses timer")
                    if w == 160:
                        border = (20, 47, 70)
                        for px, py in ((0, h//2), (w-1, h//2), (w//2, 0), (w//2, h-1)):
                            require(rgb.getpixel((px, py)) == border, f"{name} edge differs at {(px,py)}")
                        # Every pixel outside the clock text must match the independent source.
                        center = rgb.getpixel((w//2, h//2))
                        for py in range(h):
                            for px in range(w):
                                if 5 <= px <= 140 and 4 <= py <= 30: continue
                                expected_pixel = center if 2 <= px < w-2 and 2 <= py < h-2 else border
                                require(rgb.getpixel((px, py)) == expected_pixel, f"{name} contaminated at {(px,py)}")
            if case == "timer":
                frequency = events("environment")[0]["qpc_frequency"]
                observed = {}
                for e in events("preview_rendered"):
                    color = (e["red"], e["green"], e["blue"])
                    if color in palette:
                        observed.setdefault(color, (e["presented_ticks"]-palette[color])*1000/frequency)
                require(len(observed) >= 20, f"only {len(observed)} matching timer paints")
                values = sorted(observed.values())
                metrics.update(latency_ms=list(observed.values()), p95_ms=values[math.ceil(.95*len(values))-1])
                require(min(values) >= 0 and metrics["p95_ms"] <= 200, str(metrics))
            return metrics
        finally:
            main = own_window(app.pid, "ScreenTrigger · 框选探针")
            if main: user32.PostMessageW(main, 0x10, 0, 0)
            try: app.wait(3)
            except subprocess.TimeoutExpired:
                app.terminate()
                app.wait(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--composed-capture", action="store_true", help="exercise the live pre-2004 capture path")
    parser.add_argument("--display", help="display device name, defaults to primary")
    parser.add_argument("--cases", nargs="+", choices=("timer","reverse","minimum","escape","close","failure","retry"),
                        default=["timer","reverse","minimum","escape","close","failure","retry"])
    args = parser.parse_args()
    env = base_environment(set_per_monitor_v2())
    display = next(m for m in env["monitors"] if (m["device"] == args.display if args.display else m["primary"]))
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    old_focus = user32.GetForegroundWindow()
    old_cursor = wt.POINT()
    user32.GetCursorPos(ctypes.byref(old_cursor))
    target = TimerTarget(display["physical_rect"])
    results = {}
    try:
        for case in args.cases:
            try: results[case] = dict(status="PASS", **exercise(args.exe.resolve(), out, case, display["physical_rect"], target, args.composed_capture))
            except Exception as ex:
                results[case] = dict(status="FAIL", error=f"{type(ex).__name__}: {ex}")
            print(case, results[case], flush=True)
    finally:
        target.destroy()
        user32.SetCursorPos(old_cursor.x, old_cursor.y)
        if old_focus: user32.SetForegroundWindow(old_focus)
        (out / "results.json").write_text(json.dumps(dict(environment=env, scenarios=results, paints=target.frames),
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    return int(any(row["status"] != "PASS" for row in results.values()))


if __name__ == "__main__":
    sys.exit(main())
