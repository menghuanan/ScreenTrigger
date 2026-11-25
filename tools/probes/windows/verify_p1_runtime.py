"""Exercise the desktop ABI, real sidecar, Win32 binding, input and registered global hotkeys.

Only the separate test process's foreground edit control receives test text or key chords.
The data directory is exclusive to this attempt. Use the production WinUI probe for XAML.
"""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
import psutil

import common

user = common.user32
user.SetFocus.argtypes = [ct.c_void_p]
user.SetForegroundWindow.argtypes = [ct.c_void_p]
user.MoveWindow.argtypes = [ct.c_void_p, ct.c_int, ct.c_int, ct.c_int, ct.c_int, ct.c_int]
user.ShowWindow.argtypes = [ct.c_void_p, ct.c_int]
user.WindowFromPoint.argtypes = [wt.POINT]
user.WindowFromPoint.restype = ct.c_void_p
user.GetAsyncKeyState.argtypes = [ct.c_int]
user.GetAsyncKeyState.restype = ct.c_short
user.GetForegroundWindow.restype = ct.c_void_p
user.keybd_event.argtypes = [ct.c_ubyte, ct.c_ubyte, wt.DWORD, ct.c_size_t]


def target_window(channel):
    common.set_per_monitor_v2()
    title = "ScreenTrigger P1 " + str(uuid4())
    window = common.MiniWindow(title, 80, 100, 560, 300)
    edit = common.create_edit_child(window.hwnd, "", 20, 20, 420, 110)
    try:
        user.SetForegroundWindow(window.hwnd)
        user.SetFocus(edit)
        channel.send((window.hwnd, edit, title))
        while True:
            window.pump(8)
            if not channel.poll():
                continue
            cmd = channel.recv()
            if cmd == "quit":
                break
            if cmd == "move":
                user.MoveWindow(window.hwnd, 135, 150, 580, 310, True)
            if cmd == "minimize":
                user.ShowWindow(window.hwnd, 6)
            if cmd == "focus":
                user.ShowWindow(window.hwnd, 9)
                user.SetForegroundWindow(window.hwnd)
                user.SetFocus(edit)
            channel.send(common.edit_get_text(edit))
    finally:
        window.destroy()


Receive = ct.CFUNCTYPE(None, ct.c_void_p, ct.c_size_t)
Window = ct.CFUNCTYPE(ct.c_int, ct.c_void_p, ct.c_size_t, ct.c_void_p, ct.c_size_t)


class Host:
    def __init__(self, library, folder):
        self.events = []
        self.dll = ct.CDLL(str(library.resolve()))
        self.receiver = Receive(lambda p, n: self.events.append(json.loads(ct.string_at(p, n))))
        self.window = Window(lambda p, n, out, cap: -1)
        signatures = {"open": ([ct.c_uint32, Receive], ct.c_void_p),
                      "attach_window": ([ct.c_void_p, Window], ct.c_int),
                      "call": ([ct.c_void_p, ct.c_char_p, ct.c_size_t, Receive], ct.c_int),
                      "begin_close": ([ct.c_void_p], None), "close": ([ct.c_void_p], None)}
        for name, (args, result) in signatures.items():
            fn = getattr(self.dll, "st_host_" + name)
            fn.argtypes, fn.restype = args, result
        os.environ["SIDECAR_DATA_DIR"] = str(folder.resolve() / "data")
        os.environ["SIDECAR_PYTHON"] = sys.executable
        self.ptr = self.dll.st_host_open(1, self.receiver)
        assert self.ptr and self.dll.st_host_attach_window(self.ptr, self.window) == 0

    def close(self):
        self.dll.st_host_begin_close(self.ptr)
        self.dll.st_host_close(self.ptr)

    def call(self, op, **params):
        payload = json.dumps(dict(op=op, **params), ensure_ascii=False).encode()
        records = []
        reply = Receive(lambda p, n: records.append(json.loads(ct.string_at(p, n))))
        assert self.dll.st_host_call(self.ptr, payload, len(payload), reply) == 0 and records
        wire = records[0]
        if not wire["success"]:
            raise Fault(wire["error"])
        return wire["data"]

    def request(self, command, **params):
        return self.call("request", session_id=self.session, id=str(uuid4()), command=command, params=params)

    def save(self, source):
        return self.request("save_script", name=str(uuid4()), source=source, parameters=[])["script"]["id"]

    def state(self, execution):
        return self.request("get_script_state", execution_id=execution)["state"]

    def until(self, execution, status):
        def read():
            state = self.state(execution)
            if state["status"] in ("failed", "cancelled", "completed") and state["status"] != status:
                raise AssertionError(state)
            return state if state["status"] == status else None
        return wait(read, "execution " + status)


class Fault(Exception):
    def __init__(self, error):
        super().__init__(error)
        self.code = error["code"]


def expect(code, action):
    try:
        action()
    except Fault as exc:
        assert exc.code == code, exc
        return
    raise AssertionError("Expected " + code)


def wait(fn, message, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        result = fn()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError("Timed out: " + message)


def chord(hwnd, key):
    assert user.GetForegroundWindow() == hwnd, "test target lost foreground"
    keys = [0x11, 0x12, key]
    assert all(not user.GetAsyncKeyState(k) & 0x8000 for k in keys), "user holds a shortcut key"
    down = []
    try:
        for vk in keys:
            user.keybd_event(vk, 0, 0, 0)
            down.append(vk)
    finally:
        for vk in reversed(down):
            user.keybd_event(vk, 0, 2, 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    common.set_per_monitor_v2()
    channel, child_end = mp.Pipe()
    process = mp.Process(target=target_window, args=(child_end,), daemon=True)
    previous = user.GetForegroundWindow()
    cursor = wt.POINT()
    user.GetCursorPos(ct.byref(cursor))
    process.start()
    host = None
    steps = {}
    def observe(name, value):
        steps[name] = value
        print(name, json.dumps(value, ensure_ascii=False), flush=True)
    try:
        assert channel.poll(8), "target startup"
        hwnd, edit, title = channel.recv()
        def target(command):
            channel.send(command)
            assert channel.poll(2)
            return channel.recv()
        def focus():
            target("focus")
            wait(lambda: user.GetForegroundWindow() == hwnd, "dedicated foreground")
            x, y = common.client_screen_origin_helper(edit)
            assert user.SetCursorPos(x + 20, y + 20)
            assert user.WindowFromPoint(wt.POINT(x+20, y+20)) == edit
            return x+20, y+20
        host = Host(args.library, args.output)
        status = host.call("start")
        host.session = status["session_id"]
        hello = host.request("hello")
        caps = {c["id"]: c for c in hello["capabilities"]}
        observe("capabilities", caps)
        assert all(caps[k]["state"]=="available" for k in ("lua_script","capture","input"))
        display = host.request("get_display_list")["displays"][0]["id"]
        window = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
        assert window["pid"] == process.pid
        bound = host.request("bind_window", window_id=window["window_id"])["binding"]["binding_id"]
        focus()
        initial = host.request("get_window_info", window_id=window["window_id"])["window"]
        target("move")
        changed = wait(lambda: (w if (w:=host.request("get_window_info",window_id=window["window_id"])["window"])["geometry_revision"] > initial["geometry_revision"] else None), "geometry")
        observe("geometry", [initial, changed])
        x, y = focus()
        def run(script, binding=True, **options):
            scope = {"binding_id":bound} if binding else {"display_id":display}
            return host.request("execute_script", script_id=script, **scope, **options)["execution_id"]
        text = "P1窗口✓"
        src = ('local x,y=get_mouse_position(); local rgb=get_pixel_color(x,y); '
               'local img=capture_screen(); release_image(img); click_at(x,y); type_text("%s",0); '
               'return {x=x,y=y,rgb=rgb}' % text)
        result = host.until(run(host.save(src)), "completed")
        assert result["result"]["x"] == x and result["result"]["y"] == y, result
        wait(lambda: text in target("text"), "Unicode edit contents")
        observe("dedicated_input", dict(state=result, text=target("text")))
        outside = host.save('local x,y=get_mouse_position(); local ok,e=pcall(click_at, x+1600, y); '
                            'local u,v=get_mouse_position(); return {code=e.code, unchanged=(x==u and y==v)}')
        refused = host.until(run(outside), "completed")
        assert refused["result"] == {"code":"REGION_LIMIT_VIOLATION", "unchanged":True}, refused
        observe("client_boundary", refused["result"])
        shot = host.request("capture_window", binding_id=bound)["image"]
        rgb = result["result"]["rgb"]
        params = {"color_space":"rgb", "color":rgb, "min_pixel_count":1}
        draft = dict(name="Product runtime color", image_id=shot["image_id"], anchor_condition_id="c1",
                     conditions={"op":"all", "children":[{"type":"color", "id":"c1", "params":params}]})
        target_id = host.request("create_target", draft=draft)["target"]["id"]
        host.request("release_image", image_id=shot["image_id"])
        script = host.save('local ok,h=find_target("' + target_id + '"); return {ok=ok, sample=h[1].sample_no}')
        hit = host.until(run(script), "completed")
        assert hit["result"] == {"ok":True, "sample":1}, hit
        until = host.until(run(script, mode="until_target", stop_target_id=target_id), "completed")
        assert until["iteration"] == 0 and until["reason"] == "target_detected", until
        count = host.until(run(script, mode="count", iterations=2), "completed")
        assert count["iteration"] == 2
        observe("real_capture_and_vision_modes", dict(sample=hit["result"], until=until, count=count))
        config = host.request("get_config")["config"]
        role = "stop_script"
        assert user.RegisterHotKey(None, 402, 3, 0x77)
        try:
            expect("HOTKEY_CONFLICT", lambda: host.request("update_config", expected_revision=config["revision"],
                   patch={"hotkeys":{role:"Ctrl+Alt+F8"}}))
            assert host.request("get_config")["config"] == config
        finally:
            user.UnregisterHotKey(None, 402)
        observe("hotkey_conflict_preserves_disk", config["hotkeys"])
        live = host.save('wait(20)')
        ids = [run(live, binding=False) for _ in range(3)]
        for id_ in ids:
            host.until(id_, "running")
        expect("BUSY", lambda:run(live, binding=False))
        host.call("select_execution", session_id=host.session, execution_id=ids[1])
        focus()
        chord(hwnd, ord("P"))
        host.until(ids[1], "paused")
        observe("pause_selected", [host.state(i)["status"] for i in ids])
        assert steps["pause_selected"] == ["running","paused","running"]
        chord(hwnd, ord("P"))
        host.until(ids[1], "running")
        chord(hwnd, ord("X"))
        observe("stop_all", [host.until(i,"cancelled")["status"] for i in ids])
        focus()
        assert not user.GetAsyncKeyState(0x75) & 0x8000
        typing = run(host.save('key_down("f6"); type_text(string.rep("z",80),.06)'))
        wait(lambda: user.GetAsyncKeyState(0x75) & 0x8000 and target("text").count("z") >= 3, "held F6 and text")
        before_stop = time.perf_counter()
        chord(hwnd, ord("X"))
        host.until(typing,"cancelled")
        stop_ms = (time.perf_counter() - before_stop) * 1000
        assert not user.GetAsyncKeyState(0x75) & 0x8000
        after = target("text")
        time.sleep(.25)
        assert after == target("text") and after.count("z") < 80
        observe("stop_during_input", dict(terminal_ms=stop_ms, delivered=after.count("z"), unchanged_after_ms=250))
        focus()
        holder = run(host.save('key_down("f6"); wait(30)'))
        host.until(holder, "running")
        wait(lambda: user.GetAsyncKeyState(0x75) & 0x8000, "owned F6 before crash")
        reported = psutil.Process(host.call("status")["pid"])
        expected_dir = (args.output/"data").resolve()
        victim = None
        for candidate in reversed([reported] + reported.children(recursive=True)):
            argv = candidate.cmdline()
            if (host.session in argv and "--data-dir" in argv
                    and Path(argv[argv.index("--data-dir")+1]).resolve() == expected_dir
                    and os.getpid() in [p.pid for p in candidate.parents()]):
                victim = candidate
                break
        assert victim, "could not verify ownership of this attempt's sidecar"
        crash_time = time.perf_counter()
        victim.kill()
        wait(lambda: not (user.GetAsyncKeyState(0x75) & 0x8000), "release held key after sidecar failure", 3)
        released_ms = (time.perf_counter()-crash_time)*1000
        wait(lambda:host.call("status")["state"].lower()=="exited", "supervisor detects exit")
        old_session = host.session
        host.session = host.call("start")["session_id"]
        assert old_session != host.session
        assert host.request("get_bound_window")["binding"] is None
        window = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
        bound = host.request("bind_window", window_id=window["window_id"])["binding"]["binding_id"]
        observe("crash_cleanup_and_restart", dict(release_ms=released_ms, new_session=host.session))
        focus()
        worker = run(host.save("while true do end"))
        host.until(worker, "running")
        target("minimize")
        host.until(worker,"paused")
        focus()
        host.request("resume_script", execution_id=worker)
        host.until(worker, "running")
        channel.send("quit")
        process.join(4)
        closed = host.until(worker,"failed")
        assert closed["error"]["code"] == "WINDOW_NOT_FOUND", closed
        observe("window_loss", closed)
    finally:
        if host:
            host.close()
            (args.output/"events.json").write_text(json.dumps(host.events, ensure_ascii=False, indent=2), encoding="utf8")
        (args.output/"observations.json").write_text(json.dumps(steps, ensure_ascii=False, indent=2), encoding="utf8")
        if process.is_alive():
            channel.send("quit")
            process.join(2)
        if process.is_alive():
            process.terminate()
        user.SetCursorPos(cursor.x, cursor.y)
        if previous and user.IsWindow(previous):
            user.SetForegroundWindow(previous)


if __name__ == "__main__":
    main()
