"""Check a stale window selection against genuine HWND reuse by a different test process."""
import argparse
from collections import Counter
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def fixture_windows():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateWindowExW.restype = wintypes.HWND
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    return user


def fixture(mode):
    user = fixture_windows()
    live = set()

    def create(parent=None):
        # A single pixel off-screen, with no taskbar entry or activation.
        hwnd = user.CreateWindowExW(0x08000080, "STATIC", "", 0x80000000,
                                    -32000, -32000, 1, 1, parent, None, None, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        live.add(hwnd)
        return hwnd

    def destroy(hwnd):
        if not user.DestroyWindow(hwnd):
            raise ctypes.WinError(ctypes.get_last_error())
        live.remove(hwnd)

    try:
        if mode == "original":
            for index in range(128):
                hwnd = create()
                user.SetWindowTextW(hwnd, f"ScreenTrigger identity fixture {os.getpid()}-{index}")
                user.ShowWindow(hwnd, 8)  # SW_SHOWNA
            print(json.dumps(sorted(live)), flush=True)
            sys.stdin.readline()
            return
        params = json.loads(sys.stdin.readline())
        targets = set(params["handles"])
        deadline = time.monotonic() + params["seconds"]
        slots = Counter()
        attempts = 0
        # Message-only windows share the OS handle table and avoid desktop repaints
        # while waiting for Windows to assign an identical generation and slot.
        while time.monotonic() < deadline:
            hwnd = create(-3)  # HWND_MESSAGE
            attempts += 1
            slots[hwnd & 0xffff] += 1
            if hwnd in targets:
                user.SetParent(hwnd, None)
                user.SetWindowTextW(hwnd, f"ScreenTrigger new identity {os.getpid()}")
                user.ShowWindow(hwnd, 8)
                print(json.dumps(dict(hwnd=hwnd, attempts=attempts, reused=True)), flush=True)
                sys.stdin.readline()
                return
            destroy(hwnd)
        print(json.dumps(dict(reused=False, attempts=attempts,
                              matching_slots=sum(slots[h & 0xffff] for h in targets))), flush=True)
    finally:
        for hwnd in tuple(live):
            destroy(hwnd)


def check(output, seconds):
    from verify_p1_runtime import Host, Fault
    output.mkdir(exist_ok=False)
    old = None
    new = None
    host = Host(Path(sys.executable).parent / "screen_trigger_lib.dll", output)
    result = {}

    def worker(mode):
        return subprocess.Popen([sys.executable, "-I", "-B", __file__, "--worker", mode],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def send(child, value):
        child.stdin.write(json.dumps(value) + "\n")
        child.stdin.flush()

    try:
        host.session = host.call("start")["session_id"]
        old = worker("original")
        handles = json.loads(old.stdout.readline())
        entries = host.request("list_windows")["windows"]
        by_handle = {int(w["handle"], 16): w for w in entries if w["pid"] == old.pid}
        assert set(by_handle) == set(handles)
        first = by_handle[handles[0]]
        old_binding = host.request("bind_window", window_id=first["window_id"])["binding"]
        result.update(original_pid=old.pid, count=len(handles), original_binding=old_binding)
        new = worker("replacement")
        send(old, "close")
        assert old.wait(10) == 0
        send(new, dict(handles=handles, seconds=seconds))
        allocation = json.loads(new.stdout.readline())
        result["allocation"] = allocation
        if not allocation["reused"]:
            raise RuntimeError("Windows did not reassign any identical numeric HWND during the observation")
        old_entry = by_handle[allocation["hwnd"]]
        result["original"] = old_entry
        # Do this before another list_windows call would prune historical selections.
        try:
            host.request("bind_window", window_id=old_entry["window_id"])
        except Fault as exc:
            result["stale_error"] = exc.code
            assert exc.code == "WINDOW_NOT_FOUND"
        else:
            raise AssertionError("Stale window selection accepted")
        assert host.request("get_bound_window")["binding"] is None
        replacement = next(w for w in host.request("list_windows")["windows"]
                           if w["pid"] == new.pid and int(w["handle"], 16) == allocation["hwnd"])
        result["replacement"] = replacement
        assert replacement["window_id"] != old_entry["window_id"]
        assert replacement["pid"] != old_entry["pid"]
        assert replacement["process_started_at"] != old_entry["process_started_at"]
        new_binding = host.request("bind_window", window_id=replacement["window_id"])["binding"]
        assert new_binding["binding_id"] != old_binding["binding_id"]
        result["new_binding"] = new_binding
    finally:
        for child in (old, new):
            if child and child.poll() is None:
                child.stdin.close()
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    child.wait(5)
        host.close()
        (output / "observations.json").write_text(json.dumps(result, indent=2), "utf8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--worker", choices=("original", "replacement"))
    args = parser.parse_args()
    if args.worker:
        fixture(args.worker)
    elif args.output:
        check(args.output, args.seconds)
    else:
        parser.error("Supply a new output directory")
