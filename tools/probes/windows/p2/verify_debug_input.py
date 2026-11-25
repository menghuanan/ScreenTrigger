"""P2 desktop ABI integration against a dedicated Win32 edit, using only its bound client."""
import ctypes as ct
import json
import multiprocessing as mp
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path.cwd() / "tools/probes/windows"))
from verify_p1_runtime import Host, target_window, common, user, chord, wait, Fault, wt

def main(folder, library):
    folder.mkdir(exist_ok=False)
    common.set_per_monitor_v2()
    pipe, child = mp.Pipe()
    proc = mp.Process(target=target_window, args=(child,), daemon=True)
    original = user.GetForegroundWindow()
    cursor = wt.POINT()
    user.GetCursorPos(ct.byref(cursor))
    proc.start()
    host = None
    observations = {}
    try:
        assert pipe.poll(8)
        hwnd, edit, title = pipe.recv()
        def window(command):
            pipe.send(command)
            assert pipe.poll(2)
            return pipe.recv()
        window("focus")
        wait(lambda:user.GetForegroundWindow() == hwnd, "dedicated focus")
        host = Host(library, folder)
        host.session = host.call("start")["session_id"]
        row = next(w for w in host.request("list_windows")["windows"] if w["pid"] == proc.pid and w["title"] == title)
        bound = host.request("bind_window", window_id=row["window_id"])["binding"]["binding_id"]
        def launch(source, line):
            script = host.save(source)
            digest = host.request("load_script", script_id=script)["script"]["source_sha256"]
            host.request("add_breakpoint", script_id=script, source_hash=digest, line=line)
            return host.request("execute_script", script_id=script, binding_id=bound, debug=True)["execution_id"]

        x, y = common.client_screen_origin_helper(edit)
        assert user.SetCursorPos(x + 20, y + 20) and user.WindowFromPoint(wt.POINT(x+20,y+20)) == edit
        assert not user.GetAsyncKeyState(0x75) & 0x8000
        before = window("text")
        run = launch('key_down("f6")\nwait(.35)\ntype_text("P2",0)', 3)
        wait(lambda: user.GetAsyncKeyState(0x75) & 0x8000, "F6 held at line 2")
        paused = host.until(run, "debug_paused")
        wait(lambda: not user.GetAsyncKeyState(0x75) & 0x8000, "F6 released before continuing")
        assert window("text") == before
        frames = host.request("get_stack", execution_id=run)["frames"]
        chord(hwnd, ord("X"))
        terminal = host.until(run, "cancelled")
        time.sleep(.2)
        assert window("text") == before and not user.GetAsyncKeyState(0x75) & 0x8000
        observations["breakpoint_hotkey"] = dict(paused=paused, frames=frames, terminal=terminal, no_new_text=True, owned_f6_released=True)

        run = launch('local x=1\nclick_at(-9000,-9000)', 2)
        host.until(run, "debug_paused")
        window("minimize")
        try:
            host.request("continue_execution", execution_id=run)
        except Fault as exc:
            assert exc.code == "WINDOW_UNAVAILABLE", exc
        else:
            raise AssertionError("resumed while minimized")
        assert host.state(run)["status"] == "debug_paused"
        window("focus")
        wait(lambda:user.GetForegroundWindow() == hwnd, "foreground after restore")
        host.request("continue_execution", execution_id=run)
        failed = host.until(run,"failed")
        assert failed["error"]["code"] == "REGION_LIMIT_VIOLATION", failed
        observations["bounds_and_focus"] = failed
    finally:
        (folder / "observations.json").write_text(json.dumps(observations,ensure_ascii=False,indent=2),encoding="utf-8")
        if host: host.close()
        if proc.is_alive(): pipe.send("quit")
        proc.join(4)
        if proc.is_alive(): proc.terminate(); proc.join()
        user.SetCursorPos(cursor.x, cursor.y)
        if original: user.SetForegroundWindow(original)
    print(json.dumps(observations,ensure_ascii=False))

if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else
         Path("dist/desktop/bin/ScreenTrigger/debug_win-x64/screen_trigger_lib.dll"))
