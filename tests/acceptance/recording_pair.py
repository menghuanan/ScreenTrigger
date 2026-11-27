"""Verify desktop protocol input and the ordinary desktop's printable-key exclusion."""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recording_ime import Host, common, editor, user, wait
from portable_media import marker_for_host


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_english(layout):
    if layout["primary_language"] != 9 or (layout["is_ime"] and layout["ime_open"]):
        raise RuntimeError(f"The focused fixture requires an English keyboard layout: {layout}")


def verify_draft(before, finished, expected_interruptions=()):
    actions = finished["actions"]
    assert before == 1 and finished["action_count"] == len(actions) == before + 1, finished
    assert finished["interrupted"] == list(expected_interruptions), finished
    assert [row["action"]["kind"] for row in actions] == ["click", "key_sequence"], actions
    edges = actions[-1]["action"]["events"]
    assert [(e.get("key"), e["down"]) for e in edges] == [("a", True), ("a", False)], edges
    assert 0 <= edges[0]["offset_ms"] <= edges[1]["offset_ms"], edges


def trial(dll, folder, mode, scope, result):
    title = "ScreenTrigger English edit " + uuid4().hex
    parent, child = mp.Pipe()
    protocol = mode == "consenting"
    process = mp.Process(target=editor, args=(child, title, os.getpid(), protocol))
    host = None
    process.start()
    child.close()

    def ask(command):
        parent.send(command)
        if not parent.poll(5):
            raise RuntimeError(f"The test edit did not respond to {command}")
        return parent.recv()

    try:
        if not parent.poll(8):
            raise RuntimeError("The test edit did not start")
        top, edit = parent.recv()
        user.GetPropW.argtypes = [wt.HWND, wt.LPCWSTR]
        user.GetPropW.restype = wt.HANDLE
        result["protocol_property"] = bool(user.GetPropW(top, "ScreenTrigger.RecordingInput.v1"))
        assert result["protocol_property"] == protocol

        def click():
            rect = wt.RECT()
            assert user.GetWindowRect(edit, ct.byref(rect))
            point = next((p for y in range(rect.top + 8, rect.bottom, 18)
                          for x in range(rect.left + 8, rect.right, 18)
                          if user.WindowFromPoint(p := wt.POINT(x, y)) == edit), None)
            if point is None or not user.SetCursorPos(point.x, point.y):
                raise RuntimeError("The real edit has no visible click point")
            if user.WindowFromPoint(point) != edit:
                raise RuntimeError("The edit's click point was covered")
            user.mouse_event(2, 0, 0, 0, 0)
            try:
                time.sleep(.035)
            finally:
                user.mouse_event(4, 0, 0, 0, 0)
            time.sleep(.065)

        def check_focus():
            if ask("state")["focus"] != edit or user.GetForegroundWindow() != top:
                raise RuntimeError("The fixture does not have the foreground keyboard focus")

        def send_a():
            check_focus()
            if user.GetAsyncKeyState(0x41) & 0x8000:
                raise RuntimeError("The test key is already held")
            user.keybd_event(0x41, 0x1e, 0, 0)
            try:
                time.sleep(.10)
                check_focus()
            finally:
                user.keybd_event(0x41, 0x1e, 2, 0)
            time.sleep(.08)

        def receive_a(stage):
            ask("clear")
            send_a()
            received = ask("state")["truth"]
            result[stage] = received
            if received != [(0x41, 0x1e, True), (0x41, 0x1e, False)]:
                raise RuntimeError("The edit did not receive exactly VK_A down/up with scan 30")

        host = Host(dll, folder)
        host.session = host.call("start")["session_id"]
        host.request("hello")
        display = next(d["id"] for d in host.request("get_display_list")["displays"] if d["primary"])
        context = {"display_id": display}
        if scope == "window":
            win = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
            assert win["pid"] == process.pid
            context = {"binding_id": host.request("bind_window", window_id=win["window_id"])["binding"]["binding_id"]}
        elif scope == "region":
            r = wt.RECT()
            assert user.GetWindowRect(edit, ct.byref(r))
            context["region"] = dict(space="screen_physical", display_id=display, x=r.left,
                                     y=r.top, width=r.right-r.left, height=r.bottom-r.top)
        click()
        click()
        check_focus()
        result["original_layout"] = ask("layout")
        require_english(ask("english"))

        def start():
            old = ask("state")["offered"]
            args = {"recording_id": host.request("start_recording", context=context)["recording_id"]}
            click()
            wait(lambda: host.request("get_recording_state", **args)["action_count"] >= 1, "setup click")
            if protocol:
                wait(lambda: ask("state")["offered"] not in (old, None), "session-specific recording offer")
            return args

        recording = start()
        before = host.request("get_recording_state", **recording)
        result["keyboard_source"] = before["keyboard_source"]
        result["before"] = before["action_count"]
        result["stage"] = "target_input"
        result["before_key_layout"] = ask("layout")
        require_english(result["before_key_layout"])
        receive_a("received")
        result["after_key_layout"] = ask("layout")
        require_english(result["after_key_layout"])
        result["stage"] = "draft"
        finished = host.request("finish_recording", **recording)
        result.update(after=finished["action_count"], actions=finished["actions"], interrupted=finished["interrupted"])
        if protocol:
            verify_draft(result["before"], finished)
            assert result["keyboard_source"] == "focused_window_messages", result["keyboard_source"]
        else:
            assert result["before"] == finished["action_count"] == 1, finished
            assert [entry["action"]["kind"] for entry in finished["actions"]] == ["click"], finished
            result["ordinary_desktop_printable_growth"] = 0
        result["stage"] = "stop"
        receive_a("received_after_stop")
        result["after_stop"] = host.request("get_recording_state", **recording)["action_count"]
        assert result["after_stop"] == finished["action_count"]

        result["stage"] = "setup"
        recording = start()
        result["stage"] = "password"
        before_mask = host.request("get_recording_state", **recording)["action_count"]
        mask = ask("password")
        if not mask["masked"]:
            raise RuntimeError("The test edit did not enter password mode")
        time.sleep(.075)
        send_a()
        result["password_test_edges"] = ask("state")["masked_a_edges"] - mask["masked_a_edges"]
        if result["password_test_edges"] != 2:
            raise RuntimeError("The masked edit did not receive both test-key edges")
        result["password_growth"] = host.request("get_recording_state", **recording)["action_count"] - before_mask
        assert result["password_growth"] == 0
        ask("remove-mask")
        result["stage"] = "cancel"
        assert host.request("cancel_recording", **recording)["action_count"] == 0
        receive_a("received_after_cancel")
        result["after_cancel"] = host.request("get_recording_state", **recording)["action_count"]
        assert result["after_cancel"] == 0
        result["stage"] = "complete"
    finally:
        try:
            if host:
                host.close()
        finally:
            try:
                if process.is_alive() and "original_layout" in result:
                    result["restored_layout"] = ask("restore-layout")
                    if result["restored_layout"]["layout"] != result["original_layout"]["layout"]:
                        raise RuntimeError("The fixture did not restore its original layout")
            finally:
                if process.is_alive():
                    parent.send("quit")
                parent.close()
                process.join(4)
                if process.is_alive():
                    process.terminate()
                    process.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dll", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=("ordinary", "consenting", "both"), default="both")
    opts = parser.parse_args()
    opts.output.mkdir(parents=True)
    dll = opts.dll.resolve()
    report = dict(dll=str(dll), dll_sha256=digest(dll), interpreter=sys.executable,
                  environment=common.base_environment(common.set_per_monitor_v2()), cases=[])
    marker = marker_for_host(dll)
    if marker.is_file():
        report["portable_marker"] = json.loads(marker.read_bytes())
    for mode in (("ordinary", "consenting") if opts.mode == "both" else (opts.mode,)):
        for scope in ("window", "region", "display"):
            folder = opts.output / f"{mode}-{scope}"
            folder.mkdir()
            result = dict(mode=mode, scope=scope, stage="setup", started_at=common.utc_now())
            try:
                trial(dll, folder, mode, scope, result)
                result["status"] = "PASS"
            except Exception as exc:
                result["status"] = "FAIL" if isinstance(exc, AssertionError) and result["stage"] in (
                    "draft", "stop", "password", "cancel") else "BLOCKED"
                result["reason"] = f"{type(exc).__name__}: {exc}"
            result["finished_at"] = common.utc_now()
            path = folder / "observed.json"
            sha = common.write_report(path, result)
            report["cases"].append(dict(mode=mode, scope=scope, status=result["status"], path=str(path), sha256=sha))
            common.write_report(opts.output / "run.json", report)
            print(mode, scope, result["status"], result.get("reason", ""), flush=True)
    statuses = [row["status"] for row in report["cases"]]
    return 2 if "BLOCKED" in statuses else int("FAIL" in statuses)


if __name__ == "__main__":
    sys.exit(main())
