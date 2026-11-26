"""Observe saved Lua and script-level defaults on a dedicated Win32 input receiver."""
import argparse
import ctypes
from ctypes import wintypes
import json
import multiprocessing
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
import common
from verify_p1_runtime import Host, user, wait


class Receiver(common.MiniWindow):
    def _wndproc(self, hwnd, message, wp, lp):
        if message in (0x200, 0x201, 0x202):
            point = wintypes.POINT(ctypes.c_short(lp & 65535).value, ctypes.c_short(lp >> 16).value)
            user.ClientToScreen(hwnd, ctypes.byref(point))
            self.events.append([time.monotonic(), message, point.x, point.y])
        if message in (0x100, 0x101) and wp == 0x77:
            self.events.append([time.monotonic(), message, wp])
        return super()._wndproc(hwnd, message, wp, lp)


def receive(pipe, name):
    common.set_per_monitor_v2()
    window = Receiver.__new__(Receiver)
    window.events = []
    window.__init__(name, 100, 140, 420, 260, popup=True)
    try:
        user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        assert user.SetWindowPos(window.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0003)
        user.SetForegroundWindow(window.hwnd)
        pipe.send(window.hwnd)
        while True:
            window.pump(8)
            if pipe.poll():
                if pipe.recv() == "quit":
                    break
                pipe.send(window.events.copy())
    finally:
        window.destroy()


def measure(library, out, legacy_only=False):
    out.mkdir(parents=True, exist_ok=False)
    common.set_per_monitor_v2()
    title = "ScreenTrigger policy " + str(uuid4())
    original_window = user.GetForegroundWindow()
    original_cursor = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(original_cursor))
    pipe, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=receive, args=(child, title), daemon=True)
    process.start()
    host = None
    observed = {}
    try:
        assert pipe.poll(10)
        hwnd = pipe.recv()
        for p in ([160, 210], [280, 260], [300, 260], [307, 258]):
            assert user.WindowFromPoint(wintypes.POINT(*p)) == hwnd
        assert user.SetCursorPos(160, 210)
        if user.GetForegroundWindow() != hwnd:
            assert user.WindowFromPoint(wintypes.POINT(160, 210)) == hwnd and not user.GetAsyncKeyState(1) & 0x8000
            user.mouse_event(2, 0, 0, 0, 0)
            user.mouse_event(4, 0, 0, 0, 0)
            wait(lambda: user.GetForegroundWindow() == hwnd, "private receiver foreground")
        host = Host(library, out)
        host.session = host.call("start")["session_id"]
        win = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
        assert win["pid"] == process.pid
        binding = host.request("bind_window", window_id=win["window_id"])["binding"]["binding_id"]
        observed["window"] = win
        observed["display"] = host.request("get_display_list")

        def events():
            pipe.send("events")
            assert pipe.poll(3)
            return pipe.recv()

        def save(source, policy=None):
            params = {} if policy is None else {"action_policy": policy}
            return host.request("save_script", name=str(uuid4()), source=source, parameters=[], **params)["script"]

        def start(script):
            assert user.GetForegroundWindow() == hwnd
            return host.request("execute_script", script_id=script["id"], binding_id=binding)["execution_id"]

        def run(label, script, expected):
            before = len(events())
            started = time.monotonic()
            eid = start(script)
            state = host.until(eid, "completed")
            time.sleep(.08)
            actual = events()[before:]
            observed[label] = dict(execution=eid, started=started, state=state, events=actual)
            assert [e[2:] for e in actual if e[1] == 0x201] == expected, observed[label]
            assert [e[2:] for e in actual if e[1] == 0x202] == expected
            return actual

        if legacy_only:
            run("legacy", save("click_at(300,260); return true"), [[300,260]])
            return
        policy = dict(version=1, curve={"c1":[0,50], "c2":[0,50]},
                      offset={"x":[7,7], "y":[-2,-2]}, delay_ms=[35,35])
        two = save("action_with_policy({version=1,curve=false,offset={x={0,0},y={0,0}}},'click_at',280,260); "
                   "click_at(300,260); return true", policy)
        actual = run("override-and-inherit", two, [[280,260], [307,258]])
        first_click = next(e for e in actual if e[1] == 0x201)
        assert first_click[0] - observed["override-and-inherit"]["started"] >= .030
        assert any(e[1] == 0x200 and e[0] > first_click[0] and e[3] > 270 for e in actual)
        run("legacy", save("click_at(300,260); return true"), [[300,260]])
        saved = save("wait(.5); click_at(300,260); return true", policy)
        before = len(events())
        eid = start(saved)
        changed = host.request("save_script", name=saved["name"], script_id=saved["id"], expected_revision=saved["revision"],
                               parameters=[], source="wait(.5); click_at(300,260); return true", action_policy={"version":1})["script"]
        state = host.until(eid,"completed")
        time.sleep(.08)
        observed["snapshot"] = dict(state=state, old_revision=saved["revision"], new_revision=changed["revision"], events=events()[before:])
        assert [e[2:] for e in observed["snapshot"]["events"] if e[1] == 0x201] == [[307,258]]
        run("next-revision", changed, [[300,260]])
        for label, overrides in (("offset", "offset={x={450,450},y={0,0}}"),
                                 ("curve", "curve={c1={0,500},c2={0,500}}")):
            script = save("local ok,e=pcall(action_with_policy,{version=1,"+overrides+"},'click_at',300,260); return e.code")
            records = run("rejected-"+label, script, [])
            assert not records and observed["rejected-"+label]["state"]["result"] == "REGION_LIMIT_VIOLATION"
        wait_script = save("key_down('F8'); action_with_policy({version=1,delay_ms={4500,4500}},'click_at',300,260); key_up('F8')")
        before = len(events())
        assert not user.GetAsyncKeyState(0x77) & 0x8000
        eid = start(wait_script)
        wait(lambda:any(e[1] == 0x100 for e in events()[before:]), "owned F8 down")
        stopped = time.monotonic()
        host.request("stop_script", execution_id=eid)
        state = host.until(eid,"cancelled")
        time.sleep(.15)
        records = events()[before:]
        observed["cancel"] = dict(state=state, stop_at=stopped, elapsed=time.monotonic()-stopped, events=records)
        assert [e[1] for e in records if e[1]!=0x200] == [0x100,0x101]
        assert not user.GetAsyncKeyState(0x77) & 0x8000
        assert len(events()) == before+len(records)
    finally:
        if host:
            host.close()
        if process.is_alive():
            pipe.send("quit")
            process.join(3)
        if process.is_alive():
            process.terminate()
            process.join(3)
        user.SetCursorPos(original_cursor.x, original_cursor.y)
        if original_window and user.IsWindow(original_window):
            user.SetForegroundWindow(original_window)
        (out/"observations.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--legacy-only", action="store_true")
    args = parser.parse_args()
    measure(args.library, args.output, args.legacy_only)
