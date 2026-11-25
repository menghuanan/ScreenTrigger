"""Exercise both input policies and manual window replacement using only owned Win32 fixtures."""
import argparse
import ctypes
from ctypes import wintypes
import json
import multiprocessing
from pathlib import Path
import sys

import common
from verify_p1_runtime import Host, chord, target_window, user, wait


def fixture():
    recv, send = multiprocessing.Pipe()
    child = multiprocessing.Process(target=target_window, args=(send,), daemon=True)
    child.start()
    if not recv.poll(10):
        raise RuntimeError("Owned window failed to start")
    hwnd, edit, title = recv.recv()
    return child, recv, hwnd, edit, title


def main(output):
    output.mkdir(exist_ok=False)
    common.set_per_monitor_v2()
    previous = user.GetForegroundWindow()
    cursor = wintypes.POINT()
    user.GetCursorPos(ctypes.byref(cursor))
    fixtures = []
    result = {"edges": []}
    first = fixture()
    fixtures.append(first[:2])
    host = Host(Path(sys.executable).parent / "screen_trigger_lib.dll", output)
    try:
        host.session = host.call("start")["session_id"]
        def bind(window):
            child, pipe, hwnd, edit, title = window
            found = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
            assert found["pid"] == child.pid
            binding = host.request("bind_window", window_id=found["window_id"])["binding"]["binding_id"]
            def focus_owned():
                user.SetForegroundWindow(hwnd)
                pipe.send("focus")
                assert pipe.poll(3)
                pipe.recv()
                return user.GetForegroundWindow() == hwnd
            if not focus_owned():
                # Windows can deny foreground activation to a background test runner.
                # A physical click is allowed only after hit-testing the exact owned edit.
                user.SetWindowPos.argtypes = [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
                flags = 0x0001 | 0x0002 | 0x0010
                assert user.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0,0,0,0,flags)
                try:
                    ox, oy = common.client_screen_origin_helper(edit)
                    point = wintypes.POINT(ox+30,oy+30)
                    assert user.SetCursorPos(point.x,point.y)
                    assert user.WindowFromPoint(point)==edit and not user.GetAsyncKeyState(1)&0x8000
                    user.mouse_event(2,0,0,0,0)
                    user.mouse_event(4,0,0,0,0)
                finally:
                    user.SetWindowPos(hwnd, ctypes.c_void_p(-2), 0,0,0,0,flags)
            wait(focus_owned, "dedicated foreground")
            geometry = host.request("get_window_info", window_id=found["window_id"])["window"]
            return child, pipe, hwnd, edit, binding, geometry
        child, pipe, hwnd, edit, binding, geometry = bind(first)
        config=host.request("get_config")["config"]
        keys={"start_selection":"Ctrl+Alt+F9","stop_script":"Ctrl+Alt+F10","pause_script":"Ctrl+Alt+F11"}
        registered=host.request("update_config",expected_revision=config["revision"],patch={"hotkeys":keys})["config"]
        assert registered["hotkeys"]==keys
        proof=host.request("execute_script",script_id=host.save("wait(10)"),binding_id=binding)["execution_id"]
        host.until(proof,"running")
        chord(hwnd,0x79)
        result["stop_shortcut"]={"registered":keys,"observed":host.until(proof,"cancelled")}
        display = next(d["id"] for d in host.request("get_display_list")["displays"] if d["primary"])
        x, y = common.client_screen_origin_helper(edit)
        x += 20
        y += 20
        client = geometry["client_rect"]
        width, height = 70, 36
        # Every possible positive dispatch remains within the private edit control.
        for point in [(x,y),(x+width,y+height),(x-1,y)]:
            assert user.WindowFromPoint(wintypes.POINT(*point)) == edit
        outside_client = (client["x"]-3, client["y"]+20)
        assert user.GetAncestor(user.WindowFromPoint(wintypes.POINT(*outside_client)), 2) == hwnd
        def run(scope, allow, label, action, success, expected=None):
            assert user.GetForegroundWindow() == hwnd
            assert user.SetCursorPos(x+10,y+10)
            before = (x+10,y+10)
            source = ('local ok,e=pcall(function() '+action+' end); local x,y=get_mouse_position(); '
                      'return {ok=ok,code=ok and "OK" or e.code,x=x,y=y}')
            eid = host.request("execute_script", script_id=host.save(source), allow_exit=allow, **scope)["execution_id"]
            state = host.until(eid,"completed")
            data = state["result"]
            assert (data["ok"],data["code"]) == (success,"OK" if success else "REGION_LIMIT_VIOLATION"),state
            assert (data["x"],data["y"]) == (expected if success else before),state
            result["edges"].append(dict(case=label,allow_exit=allow,region=scope["region"],execution_id=eid,
                                        before=before,actual=data))
        for bound in (False, True):
            scope = {"binding_id":binding} if bound else {"display_id":display}
            scope["region"] = dict(space="window_client_physical",binding_id=binding,x=x-client["x"],y=y-client["y"],
                                   width=width,height=height) if bound else dict(space="screen_physical",display_id=display,
                                                                                 x=x,y=y,width=width,height=height)
            for allow in (False,True):
                point = (x+width-1,y+height-1)
                run(scope,allow,"inside-last-pixel",f"move_to({point[0]},{point[1]},0)",True,point)
                point = (x+width,y+height-1)
                run(scope,allow,"right-edge",f"move_to({point[0]},{point[1]},0)",allow,point)
                end = (x+width-1,y+5)
                run(scope,allow,"drag-from-outside",f"drag({x-1},{y+5},{end[0]},{end[1]},0.1)",allow,end)
                if bound:
                    run(scope,allow,"nonclient-owned-border",f"move_to({outside_client[0]},{outside_client[1]},0)",False)
        script = host.save("wait(30)")
        active=host.request("execute_script",script_id=script,binding_id=binding)["execution_id"]
        host.until(active,"running")
        pipe.send("quit")
        child.join(4)
        assert not child.is_alive()
        lost = host.until(active,"failed")
        assert lost["error"]["code"]=="WINDOW_NOT_FOUND"
        next_window=fixture()
        fixtures.append(next_window[:2])
        replacement=bind(next_window)
        new_binding = replacement[4]
        assert new_binding!=binding and replacement[5]["window_id"]!=geometry["window_id"]
        new=host.request("execute_script",script_id=host.save("return 42"),binding_id=new_binding)["execution_id"]
        completed=host.until(new,"completed")
        assert completed["result"]==42 and host.state(active)["status"]=="failed"
        result["replacement"]=dict(old=geometry,new=replacement[5],lost=lost,new_root=completed,operation="explicit bind_window")
    finally:
        (output/"observations.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),"utf8")
        host.close()
        for child, pipe in fixtures:
            if child.is_alive():
                pipe.send("quit")
                child.join(3)
            if child.is_alive():
                child.terminate()
                child.join(3)
        user.SetCursorPos(cursor.x,cursor.y)
        if previous and user.IsWindow(previous):
            user.SetForegroundWindow(previous)

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output",type=Path)
    main(parser.parse_args().output)
