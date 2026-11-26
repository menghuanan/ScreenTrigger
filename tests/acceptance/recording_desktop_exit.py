"""Observe desktop recovery from an independently crashing ABI host or target process."""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording_isolated import (Host, READ_OBJECTS, SWITCH_DESKTOP, check, common, digest,
                                input_name, kernel, object_name, open_input, user)
import psutil

kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel.OpenProcess.restype = wt.HANDLE
kernel.GetProcessId.argtypes = [wt.HANDLE]
kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ct.POINTER(wt.DWORD)]
kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel.WaitForSingleObject.restype = wt.DWORD
kernel.CloseHandle.argtypes = [wt.HANDLE]


def controlled_host(pipe, dll, exe, folder):
    host = None
    try:
        host = Host(dll, folder)
        host.session = host.call("start")["session_id"]
        host.request("hello")
        state = host.request("start_recording", context={}, isolated_app={"executable": str(exe), "scope": "window"})
        pipe.send(("started", state))
        command = pipe.recv()
        if command == "crash":
            os._exit(87)  # Deliberately skip every in-process destructor on this test host.
        if command == "state":
            pipe.send(("state", host.request("get_recording_state", recording_id=state["recording_id"])))
    except BaseException as exc:
        pipe.send(("error", repr(exc)))
    finally:
        if host:
            host.close()


def receive(pipe, expected, seconds=15):
    if not pipe.poll(seconds):
        raise RuntimeError(f"Timed out waiting for host {expected}")
    kind, value = pipe.recv()
    if kind != expected:
        raise RuntimeError(f"Expected host {expected}, got {kind}: {value}")
    return value


def trial(dll, exe, folder, fault, out):
    original_handle = check(open_input(0, False, READ_OBJECTS | SWITCH_DESKTOP), "open recovery desktop")
    original = object_name(original_handle)
    out.update(original_desktop=original, fault=fault, observations=[])
    target = private = proc = pipe = None
    try:
        if object_name(user.GetThreadDesktop(kernel.GetCurrentThreadId())) != original:
            raise RuntimeError("The independent observer is not on the visible starting desktop")
        pipe, other = mp.Pipe()
        proc = mp.Process(target=controlled_host, args=(other, dll, exe, folder))
        proc.start()
        other.close()
        state = receive(pipe, "started", 30)
        private = state["isolated_app"]["desktop_name"]
        pid = state["isolated_app"]["pid"]
        out.update(host_pid=proc.pid, target_pid=pid, private_desktop=private)
        if private == original or input_name() != private:
            raise RuntimeError("The identified private desktop was not visible before the fault")
        # Pin the kernel process identity before looking up its image or parent. Never reopen
        # a PID after the crash, and never terminate a process outside this fixture's launch.
        target = check(kernel.OpenProcess(0x100000 | 0x1000 | (1 if fault == "target" else 0), False, pid),
                       "open fixture process")
        filename, size = ct.create_unicode_buffer(32768), wt.DWORD(32768)
        check(kernel.QueryFullProcessImageNameW(target, 0, filename, ct.byref(size)), "verify fixture image")
        if (kernel.GetProcessId(target) != pid or psutil.Process(pid).ppid() != proc.pid
                or not os.path.samefile(exe, filename.value)):
            raise RuntimeError("The started process does not match the owned fixture EXE and host")
        out["stage"] = "fault"
        start = time.monotonic()
        if fault == "host":
            pipe.send("crash")
            proc.join(3)
            if proc.exitcode != 87:
                raise RuntimeError(f"The test host did not exit abnormally: {proc.exitcode}")
            out["host_exit"] = proc.exitcode
        else:
            check(kernel.TerminateProcess(target, 88), "terminate verified test target")
        # Keep no handles to the private desktop between observations. Holding one artificially
        # changes whether Windows can destroy an unused desktop when the last process exits.
        deadline = start + 4
        while True:
            try:
                current = input_name()
            except RuntimeError as exc:
                current = str(exc)
            elapsed = round((time.monotonic()-start)*1000)
            out["observations"].append({"ms": elapsed, "input_desktop": current})
            if current == original or time.monotonic() >= deadline:
                break
            time.sleep(.15)
        out["automatic_return"] = current == original
        out["target_exited"] = kernel.WaitForSingleObject(target, 500) == 0
        if fault == "target":
            pipe.send("state")
            out["terminal_state"] = receive(pipe, "state")
        if not out["automatic_return"] or not out["target_exited"]:
            raise AssertionError("The original desktop and target cleanup did not recover within the observation period")
        if fault == "target" and out["terminal_state"]["status"] != "finished":
            raise AssertionError("The recorder did not finalize after its target exited")
    finally:
        try:
            if private and input_name() == private:
                check(user.SwitchDesktop(original_handle), "independent rescue to original desktop")
                out["observer_restored"] = input_name() == original
            else:
                out["observer_restored"] = False
        finally:
            if pipe:
                pipe.close()
            if proc and proc.pid:
                proc.join(2)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(3)
            if target:
                kernel.CloseHandle(target)
            user.CloseDesktop(original_handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dll", "fixture", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--fault", choices=("host", "target"), required=True)
    opts = parser.parse_args()
    opts.output.mkdir(parents=True, exist_ok=False)
    dll, exe = opts.dll.resolve(strict=True), opts.fixture.resolve(strict=True)
    out = dict(stage="setup", argv=sys.argv, started_at=common.utc_now(), dll_sha256=digest(dll),
               fixture_sha256=digest(exe), test_sha256=digest(Path(__file__)),
               environment=common.base_environment(common.set_per_monitor_v2()))
    try:
        trial(dll, exe, opts.output, opts.fault, out)
        status = 0
    except Exception as exc:
        status = 1 if isinstance(exc, AssertionError) and out["stage"] == "fault" else 2
        out["reason"] = repr(exc)
    out.update(status=("PASS", "FAIL", "BLOCKED")[status], finished_at=common.utc_now())
    common.write_report(opts.output / "observed.json", out)
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
