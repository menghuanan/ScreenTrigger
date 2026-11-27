"""Exercise a stock Win32 EDIT launched by the recorder on its own desktop.

Build recording_edit_fixture.c (see its first comment). On an interactive desktop:
    python recording_isolated.py <host DLL> <fixture EXE> <new output directory>
Use recording_pair.py with the same DLL for the consenting and original-desktop cases.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes as ct
from ctypes import wintypes as wt
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording_pair import Host, common, digest, verify_draft, wait
from portable_media import marker_for_host

user = ct.WinDLL("user32", use_last_error=True)
kernel = ct.WinDLL("kernel32", use_last_error=True)
QUERY, MASK, EDGE, ENGLISH = 0x800A, 0x800B, 0x800C, 0x800D
READ_OBJECTS, JOURNAL_PLAYBACK, WRITE_OBJECTS, SWITCH_DESKTOP = 1, 0x20, 0x80, 0x100


class ThreadInfo(ct.Structure):
    _fields_ = [("size", wt.DWORD), ("flags", wt.DWORD)] + [
        (key, wt.HWND) for key in ("active", "focus", "capture", "menu", "move", "caret")
    ] + [("rect", wt.RECT)]


class MouseInput(ct.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG), ("data", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD), ("extra", ct.c_size_t)]


class KeyInput(ct.Structure):
    _fields_ = [("vk", wt.WORD), ("scan", wt.WORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("extra", ct.c_size_t)]


class InputData(ct.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class Input(ct.Structure):
    _fields_ = [("kind", wt.DWORD), ("data", InputData)]


def bind(name, result, *args):
    func = getattr(user, name)
    func.argtypes, func.restype = args, result
    return func


open_input = bind("OpenInputDesktop", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
open_named = bind("OpenDesktopW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD)
window_visitor = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
bind("EnumDesktopWindows", wt.BOOL, wt.HANDLE, window_visitor, wt.LPARAM)
bind("SetThreadDesktop", wt.BOOL, wt.HANDLE)
bind("SwitchDesktop", wt.BOOL, wt.HANDLE)
bind("CloseDesktop", wt.BOOL, wt.HANDLE)
bind("GetThreadDesktop", wt.HANDLE, wt.DWORD)
bind("GetUserObjectInformationW", wt.BOOL, wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.POINTER(wt.DWORD))
bind("FindWindowExW", wt.HWND, wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
bind("GetWindowThreadProcessId", wt.DWORD, wt.HWND, ct.POINTER(wt.DWORD))
bind("GetGUIThreadInfo", wt.BOOL, wt.DWORD, ct.POINTER(ThreadInfo))
bind("GetForegroundWindow", wt.HWND)
bind("GetClassNameW", ct.c_int, wt.HWND, wt.LPWSTR, ct.c_int)
bind("GetAncestor", wt.HWND, wt.HWND, wt.UINT)
bind("GetWindowLongW", wt.LONG, wt.HWND, ct.c_int)
bind("GetWindowRect", wt.BOOL, wt.HWND, ct.POINTER(wt.RECT))
bind("IsWindowVisible", wt.BOOL, wt.HWND)
bind("SetWindowPos", wt.BOOL, wt.HWND, wt.HWND, ct.c_int, ct.c_int, ct.c_int, ct.c_int, wt.UINT)
bind("SetForegroundWindow", wt.BOOL, wt.HWND)
bind("GetPropW", wt.HANDLE, wt.HWND, wt.LPCWSTR)
bind("WindowFromPoint", wt.HWND, wt.POINT)
bind("GetCursorPos", wt.BOOL, ct.POINTER(wt.POINT))
bind("SetCursorPos", wt.BOOL, ct.c_int, ct.c_int)
bind("GetKeyboardLayout", wt.HANDLE, wt.DWORD)
bind("GetAsyncKeyState", ct.c_short, ct.c_int)
bind("SendMessageTimeoutW", wt.LPARAM, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM,
     wt.UINT, wt.UINT, ct.POINTER(ct.c_size_t))
bind("SendInput", wt.UINT, wt.UINT, ct.POINTER(Input), ct.c_int)
bind("PostMessageW", wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


def check(ok, operation):
    if not ok:
        raise RuntimeError(f"{operation} failed; Win32 error {ct.get_last_error()}")
    return ok


def object_name(handle):
    value, needed = ct.create_unicode_buffer(256), wt.DWORD()
    check(user.GetUserObjectInformationW(handle, 2, value, ct.sizeof(value), ct.byref(needed)), "desktop name")
    return value.value


def input_name():
    handle = check(open_input(0, False, READ_OBJECTS), "open input desktop")
    try:
        return object_name(handle)
    finally:
        check(user.CloseDesktop(handle), "close input desktop handle")


@contextmanager
def desktop_thread(name):
    handle = check(open_named(name, 0, False, READ_OBJECTS | WRITE_OBJECTS | JOURNAL_PLAYBACK), "open recording desktop")
    try:
        def attach():
            check(user.SetThreadDesktop(handle), "attach test thread")
            assert object_name(user.GetThreadDesktop(kernel.GetCurrentThreadId())) == name

        with ThreadPoolExecutor(max_workers=1, initializer=attach) as pool:
            yield lambda func, *args: pool.submit(func, *args).result()
    finally:
        check(user.CloseDesktop(handle), "close recording desktop handle")


def message(hwnd, msg, wp=0):
    reply = ct.c_size_t()
    check(user.SendMessageTimeoutW(hwnd, msg, wp, 0, 2, 500, ct.byref(reply)), f"fixture message {msg}")
    return reply.value


def window_pid(hwnd):
    pid = wt.DWORD()
    tid = check(user.GetWindowThreadProcessId(hwnd, ct.byref(pid)), "fixture identity")
    return tid, pid.value


def belongs_to_thread_desktop(root, expected_name):
    desktop = check(user.GetThreadDesktop(kernel.GetCurrentThreadId()), "query observing thread desktop")
    if object_name(desktop) != expected_name:
        return False
    found = False

    @window_visitor
    def visit(hwnd, _):
        nonlocal found
        if hwnd == root:
            found = bool(user.IsWindowVisible(hwnd))
        return True

    check(user.EnumDesktopWindows(desktop, visit, 0), "enumerate observing desktop")
    return found


class Fixture:
    def __init__(self, pid, desktop):
        self.pid, self.desktop = pid, desktop

        def find():
            hwnd = None
            while hwnd := user.FindWindowExW(None, hwnd, "ScreenTrigger.Test.OrdinaryEdit", None):
                if window_pid(hwnd)[1] == pid and user.IsWindowVisible(hwnd) and message(hwnd, QUERY):
                    return hwnd
            return None

        self.window = wait(find, "ordinary fixture startup")
        self.edit = message(self.window, QUERY)
        assert user.GetPropW(self.window, "ScreenTrigger.RecordingInput.v1") is None

    def identity(self):
        if input_name() != self.desktop:
            raise RuntimeError("The requested desktop does not own input")
        tid, pid = window_pid(self.edit)
        name = ct.create_unicode_buffer(32)
        check(user.GetClassNameW(self.edit, name, len(name)), "read fixture class")
        if (pid != self.pid or window_pid(self.window)[1] != pid or name.value.upper() != "EDIT"
                or user.GetAncestor(self.edit, 2) != self.window
                or not belongs_to_thread_desktop(self.window, self.desktop)):
            raise RuntimeError("The fixture identity or desktop changed")
        return tid

    def focused(self):
        tid = self.identity()
        thread = ThreadInfo()
        thread.size = ct.sizeof(thread)
        check(user.GetGUIThreadInfo(tid, ct.byref(thread)), "read fixture focus")
        return user.GetForegroundWindow() == self.window and thread.focus == self.edit

    def click(self, raise_window=False):
        self.identity()
        if raise_window:
            # The independent fixture on the original desktop may be behind another window.
            # Only this process's disposable test window changes its z-order.
            check(user.SetWindowPos(self.window, -1, 0, 0, 0, 0, 0x53), "raise owned test window")
            user.SetForegroundWindow(self.window)
        rect = wt.RECT()
        check(user.GetWindowRect(self.edit, ct.byref(rect)), "read fixture edit bounds")
        def visible_point():
            self.identity()
            return next((p for y in range(rect.top+5, rect.bottom, 12) for x in range(rect.left+5, rect.right, 16)
                         if user.WindowFromPoint(p := wt.POINT(x, y)) == self.edit), None)
        bounds = (rect.left, rect.top, rect.right, rect.bottom)
        point = wait(visible_point, f"visible EDIT click point for PID {self.pid} in {bounds}")
        check(user.SetCursorPos(point.x, point.y), "position cursor over verified EDIT")
        self.identity()
        if user.WindowFromPoint(point) != self.edit or user.GetAsyncKeyState(1) & 0x8000:
            raise RuntimeError("Could not confirm a safe fixture click")
        pair = (Input * 2)()
        pair[0].data.mouse.flags, pair[1].data.mouse.flags = 2, 4
        check(user.SendInput(2, pair, ct.sizeof(Input)) == 2, "submit fixed click")
        if raise_window and not self.focused():
            # An activation click may be consumed by Windows. A second verified click
            # only on the original desktop establishes focus in the disposable child.
            wait(lambda: user.GetForegroundWindow() == self.window, "owned root activated")
            self.identity()
            if user.WindowFromPoint(point) != self.edit or user.GetAsyncKeyState(1) & 0x8000:
                raise RuntimeError("Activation changed the child under the cursor")
            check(user.SendInput(2, pair, ct.sizeof(Input)) == 2, "focus activated fixture edit")
        wait(self.focused, "edit received focus")

    def check_key_target(self, masked):
        tid = self.identity()
        layout = user.GetKeyboardLayout(tid)
        if (not self.focused() or bool(message(self.edit, 0xD2)) != masked
                or (not masked and user.GetWindowLongW(self.edit, -16) & 0x20)
                or (layout & 0x3ff) != 9 or not message(self.window, QUERY, 2)):
            raise RuntimeError(f"Expected the focused English fixture with masked={masked}")
        return layout

    def send_a(self, masked=False):
        counter = 4 if masked else 1
        before = message(self.window, QUERY, counter)
        if not self.focused():
            raise RuntimeError("Fixture lost focus before English layout selection")
        tid = self.identity()
        original_layout = user.GetKeyboardLayout(tid)
        if not message(self.window, ENGLISH):
            raise RuntimeError(f"Cannot select direct English on the fixture thread; actual HKL {user.GetKeyboardLayout(tid):#x}")
        layout = self.check_key_target(masked)
        ime = message(self.window, QUERY, 3)
        if user.GetAsyncKeyState(0x41) & 0x8000:
            raise RuntimeError("A was already held")
        entry = Input()
        entry.kind = 1
        entry.data.key = KeyInput(0x41, 0x1e, 0, 0)
        check(user.SendInput(1, ct.byref(entry), ct.sizeof(Input)) == 1, "submit fixed A down")
        began = time.monotonic()
        try:
            time.sleep(.10)  # Same hold as the matching English recording_pair.py case.
        finally:
            # A layout or mask change must not swallow our release. Only send it to
            # the same dedicated fixture after checking desktop, PID, root and focus.
            wait(self.focused, "owned fixture focus to release test key", 2)
            entry.data.key.flags = 2
            check(user.SendInput(1, ct.byref(entry), ct.sizeof(Input)) == 1, "submit fixed A up")
        held_ms = (time.monotonic() - began) * 1000
        self.check_key_target(masked)
        wait(lambda: message(self.window, QUERY, counter) == before+2, "two fixed A edges delivered")
        base = (0x10000 if masked else 0) + before
        edges = [message(self.window, EDGE, index) for index in (base, base+1)]
        if edges != [0x100, 0x101]:
            raise RuntimeError(f"Unexpected receiver messages for fixed VK_A/scan 30: {edges}")
        return dict(pid=self.pid, desktop=self.desktop, focus=self.edit, original_layout=hex(original_layout),
                    layout=hex(layout), is_ime=bool(ime & 1), has_ime_context=bool(ime & 2), ime_open=bool(ime & 4),
                    vk=0x41, scan=0x1e, masked=masked, messages=edges, sent_hold_ms=held_ms)


def trial(dll, executable, folder, scope, out):
    original_handle = check(open_input(0, False, READ_OBJECTS | SWITCH_DESKTOP), "open original desktop")
    original = object_name(original_handle)
    if object_name(user.GetThreadDesktop(kernel.GetCurrentThreadId())) != original:
        user.CloseDesktop(original_handle)
        raise RuntimeError("The original test thread is not on the interactive desktop")
    previous_cursor = wt.POINT()
    check(user.GetCursorPos(ct.byref(previous_cursor)), "read original cursor")
    process = host = external = isolated = None
    try:
        process = subprocess.Popen([str(executable)])
        external = Fixture(process.pid, original)
        external.click(raise_window=True)
        host = Host(dll, folder)
        host.session = host.call("start")["session_id"]
        host.request("hello")
        display = next(d for d in host.request("get_display_list")["displays"] if d["primary"])
        context = {} if scope == "window" else {"display_id": display["id"]}
        if scope == "region":
            context["region"] = dict(space="screen_physical", display_id=display["id"], x=120, y=140, width=500, height=260)
        for ending in ("finish", "cancel"):
            out["stage"] = "start"
            state = host.request("start_recording", context=context,
                                 isolated_app=dict(executable=str(executable), scope=scope))
            rid = {"recording_id": state["recording_id"]}
            isolated = state["isolated_app"]["desktop_name"]
            pid = state["isolated_app"]["pid"]
            if isolated == original or pid == process.pid:
                raise RuntimeError("The host did not start an isolated process and desktop")
            result = {"desktop": isolated, "pid": pid}
            out[ending] = result
            with desktop_thread(isolated) as run:
                wait(lambda: input_name() == isolated, "isolated desktop active")
                fixture = run(Fixture, pid, isolated)
                run(fixture.click)
                def current(): return host.request("get_recording_state", **rid)
                wait(lambda: current()["action_count"] == 1 and current()["keyboard_available"], "armed inside fixture")
                result["before"] = current()["action_count"]
                out["stage"] = "target_input"
                result["received"] = run(fixture.send_a)
                out["stage"] = "draft"
                wait(lambda: current()["action_count"] == 2, "one completed A in draft")
                if ending == "finish":
                    out["stage"] = "password"
                    assert run(message, fixture.window, MASK, 1) and run(message, fixture.edit, 0xD2)
                    wait(lambda: not current()["keyboard_available"], "password focus closes keyboard source")
                    result["received_in_masked_edit"] = run(fixture.send_a, True)
                    result["password_count"] = current()["action_count"]
                    assert result["password_count"] == 2
                    assert run(message, fixture.window, MASK, 0) == 0
                    if scope == "display":
                        out["stage"] = "desktop_switch"
                        check(user.SwitchDesktop(original_handle), "return to original desktop")
                        wait(lambda: current()["status"] == "finished", "automatic stop after leaving the isolated desktop")
                out["stage"] = ending
                terminal = host.request(ending+"_recording", **rid)
                result["state"] = terminal
                if ending == "finish":
                    expected = [{"after": 2, "reason": "isolated_desktop_closed"}] if scope == "display" else []
                    verify_draft(1, terminal, expected)
                else:
                    assert terminal["status"] == "cancelled" and terminal["action_count"] == 0
            wait(lambda: input_name() == original, "original desktop restored")
            isolated = None
            external.click(raise_window=True)
            result["received_on_original"] = external.send_a()
            result["after_original_input"] = current()["action_count"]
            assert result["after_original_input"] == terminal["action_count"]
    finally:
        try:
            if host:
                host.close()
        finally:
            try:
                if isolated and input_name() == isolated:
                    check(user.SwitchDesktop(original_handle), "restore original desktop after test")
                if external and process.poll() is None:
                    user.PostMessageW(external.window, 0x10, 0, 0)
                if process:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=3)
                if input_name() == original:
                    user.SetCursorPos(previous_cursor.x, previous_cursor.y)
            finally:
                user.CloseDesktop(original_handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dll", "fixture", "output"):
        parser.add_argument(name, type=Path)
    opts = parser.parse_args()
    if not __debug__:
        parser.error("Acceptance requires enabled assertions")
    dll, exe = (path.resolve(strict=True) for path in (opts.dll, opts.fixture))
    opts.output.mkdir(parents=True, exist_ok=False)
    report = dict(argv=sys.argv, dll_sha256=digest(dll), fixture_sha256=digest(exe),
                  test_sha256=digest(Path(__file__)), environment=common.base_environment(common.set_per_monitor_v2()), cases=[])
    marker = marker_for_host(dll)
    if marker.is_file():
        report["portable_marker"] = json.loads(marker.read_bytes())
    for scope in ("window", "region", "display"):
        folder = opts.output / scope
        folder.mkdir()
        result = dict(scope=scope, stage="setup", started_at=common.utc_now())
        try:
            trial(dll, exe, folder, scope, result)
            result["status"] = "PASS"
        except Exception as exc:
            result["status"] = "FAIL" if result["stage"] in ("draft", "password", "finish", "cancel", "desktop_switch") else "BLOCKED"
            result["reason"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = common.utc_now()
        path = folder / "observed.json"
        report["cases"].append(dict(scope=scope, status=result["status"], path=str(path), sha256=common.write_report(path, result)))
        common.write_report(opts.output / "run.json", report)
        print(scope, result["status"], result.get("reason", ""), flush=True)
    statuses = [row["status"] for row in report["cases"]]
    return 2 if "BLOCKED" in statuses else int("FAIL" in statuses)


if __name__ == "__main__":
    sys.exit(main())
