"""Run saved Lua through the desktop ABI and inspect clicks delivered to a private Win32 window."""
import argparse
import ctypes
from ctypes import wintypes
import json
import multiprocessing
from pathlib import Path
import platform
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
import common
from verify_p1_runtime import Host, user, wait


class Receiver(common.MiniWindow):
    def __init__(self, title):
        self.events = []
        super().__init__(title, 100, 140, 420, 260, popup=True)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg in (0x0201, 0x0202):
            p = wintypes.POINT(ctypes.c_short(lparam & 65535).value, ctypes.c_short(lparam >> 16).value)
            user.ClientToScreen(hwnd, ctypes.byref(p))
            self.events.append([msg, p.x, p.y])
        return super()._wndproc(hwnd, msg, wparam, lparam)


def receiver(pipe, title):
    common.set_per_monitor_v2()
    window = Receiver(title)
    observer = None
    try:
        user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        assert user.SetWindowPos(window.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0003)
        user.SetForegroundWindow(window.hwnd)
        pipe.send(window.hwnd)
        while True:
            window.pump(12)
            if pipe.poll():
                command = pipe.recv()
                if command == "quit":
                    break
                if command == "move":
                    assert user.MoveWindow(window.hwnd, 180, 200, 520, 320, True)
                if command == "focus":
                    user.SetForegroundWindow(window.hwnd)
                if command == "other":
                    observer = common.MiniWindow(title + " Observer", 720, 180, 180, 120, popup=True)
                    user.SetForegroundWindow(observer.hwnd)
                    pipe.send(observer.hwnd)
                    continue
                pipe.send(window.events.copy())
    finally:
        if observer:
            observer.destroy()
        window.destroy()


def check(library, output):
    output.mkdir(parents=True, exist_ok=False)
    common.set_per_monitor_v2()
    title = "ScreenTrigger Anchor " + str(uuid4())
    previous = user.GetForegroundWindow()
    cursor = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(cursor))
    pipe, child_pipe = multiprocessing.Pipe()
    process = multiprocessing.Process(target=receiver, args=(child_pipe, title), daemon=True)
    process.start()
    report = dict(os=platform.platform(), python=sys.version, steps=[])
    host = None
    try:
        assert pipe.poll(10)
        hwnd = pipe.recv()
        assert hwnd and user.WindowFromPoint(wintypes.POINT(140, 180)) == hwnd
        user.SetForegroundWindow(hwnd)
        if user.GetForegroundWindow() != hwnd:
            assert user.SetCursorPos(140, 180) and not user.GetAsyncKeyState(1) & 0x8000
            assert user.WindowFromPoint(wintypes.POINT(140, 180)) == hwnd
            user.mouse_event(2, 0, 0, 0, 0)
            user.mouse_event(4, 0, 0, 0, 0)
            wait(lambda: user.GetForegroundWindow() == hwnd, "owned foreground")
        host = Host(library, output)
        host.session = host.call("start")["session_id"]
        display = next(d for d in host.request("get_display_list")["displays"] if d["primary"])
        found = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
        assert found["pid"] == process.pid
        binding = host.request("bind_window", window_id=found["window_id"])["binding"]["binding_id"]
        report.update(display=display, hwnd=hwnd, receiver_pid=process.pid, binding_id=binding, session=host.session)

        def observe(command="events"):
            pipe.send(command)
            assert pipe.poll(3)
            return pipe.recv()

        def geometry():
            return host.request("get_window_info", window_id=found["window_id"])["window"]

        def run(label, script, config, origin, expected=None):
            for p in (origin, expected or origin):
                assert user.WindowFromPoint(wintypes.POINT(*p)) == hwnd
            assert user.SetCursorPos(*origin)
            observe("focus")
            if user.GetForegroundWindow() != hwnd:
                assert user.WindowFromPoint(wintypes.POINT(*origin)) == hwnd and not user.GetAsyncKeyState(1) & 0x8000
                user.mouse_event(2, 0, 0, 0, 0)
                user.mouse_event(4, 0, 0, 0, 0)
                wait(lambda: user.GetForegroundWindow() == hwnd, "owned foreground before script")
            before = observe()
            eid = host.request("execute_script", script_id=script, **config)["execution_id"]
            state = host.until(eid, "completed")
            if expected:
                events = wait(lambda: (v if len(v := observe()) >= len(before) + 2 else None), "click messages")
                assert events[len(before):] == [[513, *expected], [514, *expected]], events
            else:
                time.sleep(.1)
                events = observe()
                assert before == events and state["result"] == "REGION_LIMIT_VIOLATION", state
            report["steps"].append(dict(case=label, script_id=script, execution_id=eid, config=config,
                                        origin=origin, expected=expected, events=events[len(before):], result=state["result"]))

        def anchor(scope, w, h, x, y, strategy="relative"):
            return (f"click_anchor({{version=1,scope='{scope}',strategy='{strategy}',"
                    f"reference_width={w},reference_height={h},x={x},y={y}}})")

        window = dict(binding_id=binding)
        saved = host.save(anchor("window", 3, 3, 1, 1) + "; return true")
        for n in range(2):
            if n:
                observe("move")
            info = geometry()
            r = info["client_rect"]
            start = [r["x"] + 15, r["y"] + 15]
            center = [r["x"] + r["width"] // 2, r["y"] + r["height"] // 2]
            report[f"geometry_{n}"] = info
            run("resized" if n else "window", saved, window, start, center)
        assert report["geometry_1"]["geometry_revision"] > report["geometry_0"]["geometry_revision"]
        roi = dict(space="screen_physical", display_id=display["id"], x=start[0], y=start[1], width=81, height=61)
        run("region", host.save(anchor("region", 3, 3, 1, 1) + "; return true"),
            dict(display_id=display["id"], region=roi), start, [start[0]+40, start[1]+30])
        dr = display["rect"]
        endpoint = [start[0]+20, start[1]+10]
        run("display", host.save(anchor("display", dr["width"], dr["height"], endpoint[0]-dr["x"],
                                        endpoint[1]-dr["y"], "offset") + "; return true"),
            dict(display_id=display["id"]), start, endpoint)
        run("old absolute", host.save(f"click_at({endpoint[0]},{endpoint[1]}); return true"), window, start, endpoint)
        refusal = "local ok,e=pcall(function() " + anchor("window", 10, 10, 10, 1) + " end); return e.code"
        run("right edge refused", host.save(refusal), window, start)
        before = observe()
        assert user.GetForegroundWindow() == hwnd and not user.GetAsyncKeyState(0x75) & 0x8000
        script = host.save('key_down("f6"); wait(1); ' + anchor("window", 3, 3, 1, 1))
        eid = host.request("execute_script", script_id=script, **window)["execution_id"]
        wait(lambda: user.GetAsyncKeyState(0x75) & 0x8000, "owned held F6")
        host.request("stop_script", execution_id=eid)
        host.until(eid, "cancelled")
        time.sleep(1.1)
        assert observe() == before and not user.GetAsyncKeyState(0x75) & 0x8000
        report["steps"].append(dict(case="stop before click", execution_id=eid, held_f6_released=True, new_clicks=0))
        script = host.save("wait(1); " + anchor("window", 3, 3, 1, 1))
        eid = host.request("execute_script", script_id=script, **window)["execution_id"]
        host.until(eid, "running")
        observer = observe("other")
        wait(lambda: user.GetForegroundWindow() == observer, "owned observer foreground")
        paused = host.until(eid, "paused")
        assert observe() == before
        host.request("stop_script", execution_id=eid)
        host.until(eid, "cancelled")
        report["steps"].append(dict(case="bound window lost focus", execution_id=eid, state=paused, new_clicks=0))
    finally:
        if host:
            host.close()
        if process.is_alive():
            pipe.send("quit")
            process.join(3)
        if process.is_alive():
            process.terminate()
            process.join(3)
        user.SetCursorPos(cursor.x, cursor.y)
        if previous and user.IsWindow(previous):
            user.SetForegroundWindow(previous)
        (output / "observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(f"{len(report['steps'])} private-window observations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    check(args.library, args.output)
