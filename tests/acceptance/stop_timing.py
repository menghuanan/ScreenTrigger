"""Time the installed Rust host's stop command and watch the dedicated Win32 edit for late input."""
import argparse
import ctypes
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
from verify_p1_runtime import Host, common, target_window, user, wait, wt


def exercise(directory):
    common.set_per_monitor_v2()
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.Process(target=target_window, args=(child,), daemon=True)
    previous = user.GetForegroundWindow()
    cursor = wt.POINT()
    user.GetCursorPos(ctypes.byref(cursor))
    process.start()
    host = None
    samples = []
    try:
        assert parent.poll(10)
        hwnd, edit, title = parent.recv()
        def window(command):
            parent.send(command)
            assert parent.poll(3)
            return parent.recv()
        window("focus")
        if user.GetForegroundWindow() != hwnd:
            pid = wt.DWORD()
            user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
            user.GetParent.argtypes = [wt.HWND]
            user.GetParent.restype = wt.HWND
            assert user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) and pid.value == process.pid
            assert user.GetParent(edit) == hwnd
            x, y = common.client_screen_origin_helper(edit)
            user.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wt.UINT]
            flags = 0x0013  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            assert user.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, flags)
            try:
                point = wt.POINT(x + 20, y + 20)
                assert user.SetCursorPos(point.x, point.y) and user.WindowFromPoint(point) == edit
                assert not user.GetAsyncKeyState(1) & 0x8000
                user.mouse_event(2, 0, 0, 0, 0)
                user.mouse_event(4, 0, 0, 0, 0)
            finally:
                user.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0, 0, 0, 0, flags)
            window("focus")
        wait(lambda: user.GetForegroundWindow() == hwnd, "dedicated edit foreground")
        host = Host(Path(sys.executable).parent / "screen_trigger_lib.dll", directory)
        host.session = host.call("start")["session_id"]
        bound_window = next(w for w in host.request("list_windows")["windows"]
                            if w["pid"] == process.pid and w["title"] == title)
        binding = host.request("bind_window", window_id=bound_window["window_id"])["binding"]["binding_id"]
        window("focus")
        x, y = common.client_screen_origin_helper(edit)
        assert user.SetCursorPos(x+20,y+20) and user.WindowFromPoint(wt.POINT(x+20,y+20)) == edit
        script = host.save('key_down("f6")\nwait(8)\ntype_text("late",0)')
        assert not user.GetAsyncKeyState(0x75) & 0x8000
        before = window("text")
        for index in range(105):
            assert user.GetForegroundWindow() == hwnd, "test window lost foreground"
            eid = host.request("execute_script", script_id=script, binding_id=binding)["execution_id"]
            try:
                wait(lambda: user.GetAsyncKeyState(0x75) & 0x8000, "owned F6 down")
            except AssertionError:
                print("F6 observation", json.dumps(host.state(eid), ensure_ascii=False), flush=True)
                raise
            started = time.perf_counter()
            host.request("stop_script", execution_id=eid)
            wait(lambda: not user.GetAsyncKeyState(0x75) & 0x8000, "owned F6 released")
            released = (time.perf_counter()-started)*1000
            host.until(eid, "cancelled")
            assert window("text") == before, "late text reached the owned edit"
            samples.append(dict(index=index, execution=eid, observed_release_ms=released))
    finally:
        (directory / "trials.json").write_text(json.dumps(samples, indent=2),"utf8")
        if host:
            host.close()
        if process.is_alive():
            parent.send("quit")
            process.join(3)
        if process.is_alive():
            process.terminate()
            process.join(3)
        user.SetCursorPos(cursor.x, cursor.y)
        if previous and user.IsWindow(previous):
            user.SetForegroundWindow(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        exercise(args.output)
        return 0
    args.output.mkdir(exist_ok=False)
    trace = args.output / "host.log"
    command = [sys.executable, "-I", "-B", "-X", "utf8", __file__, str(args.output.resolve()), "--worker"]
    with trace.open("xb") as output:
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
            env=dict(os.environ, ST_MEASURE_TIMING="1"))
    if result.returncode:
        print(trace.read_text("utf8",errors="replace")[-2500:])
        return result.returncode
    events = {}
    for kind, execution, stamp in re.findall(r"\[gate-timing\] (\w+) ([\w-]+) (\d+)", trace.read_text("utf8", errors="replace")):
        events.setdefault(execution, {}).setdefault(kind, []).append(int(stamp))
    rows = json.loads((args.output / "trials.json").read_bytes())
    assert len(rows) == 105
    for row in rows:
        timings = events[row["execution"]]
        start = timings["stop_received"][0]
        closed = next(t for t in timings["closed"] if t >= start)
        row.update(gate_ms=(closed-start)/1_000_000, after_close=sum(t>closed for t in timings["submitted"]))
    def stats(key):
        values = sorted(row[key] for row in rows[5:])
        return {f"p{p}":values[math.ceil(len(values)*p/100)-1] for p in (50,95,99,100)}
    score = dict(warmup=5,samples=100,gate_ms=stats("gate_ms"),observed_release_ms=stats("observed_release_ms"),
                 dispatched_after_close=sum(r["after_close"] for r in rows),rows=rows)
    (args.output/"metrics.json").write_text(json.dumps(score,indent=2),"utf8")
    print(json.dumps({k:v for k,v in score.items() if k!="rows"}))
    return int(score["dispatched_after_close"] != 0 or score["gate_ms"]["p95"] > 200)


if __name__ == "__main__":
    raise SystemExit(main())
