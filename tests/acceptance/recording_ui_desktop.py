"""Operate only the verified WinUI selection overlay, launched test edit and stop window."""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
import json
from pathlib import Path
import re
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording_isolated import (
    Fixture, Input, READ_OBJECTS, SWITCH_DESKTOP, belongs_to_thread_desktop, bind, check, common,
    desktop_thread, input_name, kernel, open_named, user, wait, window_pid, window_visitor,
)
from verify_selection import drag, invoke, own_window

UUID_TEXT = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def select_region(pid, main_window, original, report):
    if input_name() != original or window_pid(main_window)[1] != pid:
        raise RuntimeError("Selection requires the original desktop and WinUI owner")
    overlay = wait(lambda: own_window(pid, "ScreenTrigger · 选择区域"), "product selection overlay")
    wait(lambda: user.GetForegroundWindow() == overlay, "overlay foreground")
    if not belongs_to_thread_desktop(overlay, original):
        raise RuntimeError("The selection overlay is not on the test desktop")

    class Delay:
        def pump(self, milliseconds):
            time.sleep(milliseconds / 1000)

    # These endpoints surround the native recording_edit_fixture.c window at (120,140).
    a, b = (100, 120), (640, 450)
    report.update(overlay=overlay, start=a, end=b)
    drag(overlay, a, b, Delay())
    if input_name() != original or window_pid(overlay)[1] != pid:
        raise RuntimeError("The selection owner changed before confirmation")
    invoke(overlay, "ConfirmSelection")
    wait(lambda: not own_window(pid, "ScreenTrigger · 选择区域"), "selection closed")


def click_indicator(pid, desktop):
    get_title = bind("GetWindowTextW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
    def find_stop():
        matches = []
        @window_visitor
        def visit(hwnd, _):
            owner = wt.DWORD()
            user.GetWindowThreadProcessId(hwnd, ct.byref(owner))
            if owner.value == pid and user.IsWindowVisible(hwnd):
                name = ct.create_unicode_buffer(128)
                if user.GetClassNameW(hwnd, name, len(name)) and re.fullmatch(r"ScreenTrigger\.Recording\."+UUID_TEXT, name.value):
                    matches.append((hwnd, name.value))
            return True
        check(user.EnumDesktopWindows(user.GetThreadDesktop(kernel.GetCurrentThreadId()), visit, 0), "find owned stop window")
        if len(matches) > 1:
            raise RuntimeError("Multiple stop indicators from the same application")
        return matches[0] if matches else None
    hwnd, class_name = wait(find_stop, "active stop window")
    title = ct.create_unicode_buffer(160)
    check(get_title(hwnd, title, len(title)), "read stop control label")
    rect = wt.RECT()
    check(user.GetWindowRect(hwnd, ct.byref(rect)), "stop window rectangle")
    point = wt.POINT((rect.left+rect.right)//2, rect.bottom-6)
    def check_stop():
        if (input_name() != desktop or not belongs_to_thread_desktop(hwnd, desktop)
                or window_pid(hwnd)[1] != pid or user.WindowFromPoint(point) != hwnd):
            raise RuntimeError("The selected stop point no longer belongs to this recording window")
    check_stop()
    check(user.SetCursorPos(point.x, point.y), "move to owned stop indicator")
    check_stop()
    if user.GetAsyncKeyState(1) & 0x8000:
        raise RuntimeError("Left mouse button already held")
    pair = (Input * 2)()
    pair[0].data.mouse.flags, pair[1].data.mouse.flags = 2, 4
    check(user.SendInput(2, pair, ct.sizeof(Input)) == 2, "click owned stop indicator")
    return dict(pid=pid, hwnd=hwnd, class_name=class_name, title=title.value, point=[point.x,point.y])


def record(pid, original, executable, report):
    app = psutil.Process(pid)

    def child():
        owned = []
        for proc in app.children():
            try:
                if Path(proc.exe()).samefile(executable):
                    owned.append(proc.pid)
            except (psutil.NoSuchProcess, FileNotFoundError):
                pass
        if len(owned) > 1:
            raise RuntimeError("More than one matching fixture child")
        return owned[0] if owned else None

    target = wait(child, "new direct child of the WinUI process", 15)
    desktop = wait(lambda: (name if re.fullmatch("ScreenTrigger-Recording-"+UUID_TEXT, name := input_name()) else None),
                   "visible dedicated recording desktop", 15)
    if original == desktop:
        raise RuntimeError("Recording desktop is the original desktop")
    report.update(fixture_pid=target, desktop=desktop)
    original_handle = check(open_named(original, 0, False, READ_OBJECTS|SWITCH_DESKTOP), "open return desktop")
    verified = False
    try:
        with desktop_thread(desktop) as run:
            fixture = run(Fixture, target, desktop)
            run(fixture.identity)
            verified = True
            run(fixture.click)
            time.sleep(.15)
            report["received"] = run(fixture.send_a)
            time.sleep(.08)
            report["stop"] = run(click_indicator, pid, desktop)
        wait(lambda: input_name() == original, "return to original desktop")
        report["returned_to_original"] = True
    finally:
        if verified and input_name() == desktop:
            check(user.SwitchDesktop(original_handle), "restore original desktop after fixture failure")
        user.CloseDesktop(original_handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("environment")
    for action in ("region", "record"):
        command = sub.add_parser(action)
        command.add_argument("pid", type=int)
        command.add_argument("main_window", type=int)
        command.add_argument("original")
        command.add_argument("output", type=Path)
        if action == "record":
            command.add_argument("fixture", type=Path)
    opts = parser.parse_args()
    awareness = common.set_per_monitor_v2()
    if opts.action == "environment":
        env = common.base_environment(awareness)
        primary = next(m["device"] for m in env["monitors"] if m["primary"])
        print(json.dumps(dict(environment=env, primary=primary, desktop=input_name()), ensure_ascii=True))
        return
    report = dict(argv=sys.argv, started_at=common.utc_now())
    with opts.output.open("x", encoding="utf-8") as output:
        try:
            if opts.action == "region":
                select_region(opts.pid, opts.main_window, opts.original, report)
            else:
                record(opts.pid, opts.original, opts.fixture.resolve(strict=True), report)
            report["status"] = "PASS"
        except Exception as exc:
            report.update(status="FAILED", reason=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            report["finished_at"] = common.utc_now()
            json.dump(report, output, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
