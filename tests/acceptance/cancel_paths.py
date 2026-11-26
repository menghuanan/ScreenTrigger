"""Stop real release executions in distinct phases inside one dedicated foreground Win32 window."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

fixture_directory = Path(__file__).resolve().parent
windows_probes = fixture_directory.parents[1] / "tools/probes/windows"
sys.path[:0] = [str(fixture_directory), str(windows_probes)]
from window_edges import fixture
from verify_p1_runtime import Host, chord, common, user, wait


def exercise(output):
    common.set_per_monitor_v2()
    previous=user.GetForegroundWindow()
    original=wintypes.POINT()
    user.GetCursorPos(ctypes.byref(original))
    child,pipe,hwnd,edit,title=fixture()
    host=None
    observed={}
    def focused():
        user.SetForegroundWindow(hwnd)
        pipe.send("focus")
        assert pipe.poll(3)
        pipe.recv()
        return user.GetForegroundWindow()==hwnd
    def text():
        pipe.send("text")
        assert pipe.poll(3)
        return pipe.recv()
    try:
        host=Host(Path(sys.executable).parent/"screen_trigger_lib.dll",output)
        host.session=host.call("start")["session_id"]
        window=next(w for w in host.request("list_windows")["windows"] if w["title"]==title)
        assert window["pid"]==child.pid
        binding=host.request("bind_window",window_id=window["window_id"])["binding"]["binding_id"]
        x,y=common.client_screen_origin_helper(edit)
        x+=25
        y+=25
        if not focused():
            user.SetWindowPos.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
            flags=0x0013
            assert user.SetWindowPos(hwnd,ctypes.c_void_p(-1),0,0,0,0,flags)
            try:
                assert user.SetCursorPos(x,y) and user.WindowFromPoint(wintypes.POINT(x,y))==edit
                assert not (user.GetAsyncKeyState(1)&0x8000)
                user.mouse_event(2,0,0,0,0)
                user.mouse_event(4,0,0,0,0)
            finally:
                user.SetWindowPos(hwnd,ctypes.c_void_p(-2),0,0,0,0,flags)
        wait(focused,"dedicated foreground")
        assert user.SetCursorPos(x,y) and user.WindowFromPoint(wintypes.POINT(x+120,y+30))==edit
        assert not user.GetAsyncKeyState(0x75)&0x8000
        cfg=host.request("get_config")["config"]
        keys={"start_selection":"Ctrl+Alt+F9","stop_script":"Ctrl+Alt+F10","pause_script":"Ctrl+Alt+F11"}
        assert host.request("update_config",expected_revision=cfg["revision"],patch={"hotkeys":keys})["config"]["hotkeys"]==keys
        def start(src,debug=False,line=None):
            sid=host.save(src)
            if line is not None:
                source=host.request("load_script",script_id=sid)["script"]
                host.request("add_breakpoint",script_id=sid,source_hash=source["source_sha256"],line=line)
            return host.request("execute_script",script_id=sid,binding_id=binding,debug=debug)["execution_id"]
        def logs(eid):
            return host.request("get_execution_logs",execution_id=eid,limit=100)["items"]
        def marker(eid,message):
            return next((row for row in logs(eid) if message in row.get("message","")),None)
        def stop(case,eid,barrier,active=None):
            assert user.GetForegroundWindow()==hwnd
            before=text()
            state=host.state(eid) if active is None else active
            assert state["status"] in ("running","debug_paused"),state
            cutoff=time.perf_counter()
            host.request("stop_script",execution_id=eid)
            final=host.until(eid,"cancelled")
            wait(lambda:not user.GetAsyncKeyState(0x75)&0x8000 and not user.GetAsyncKeyState(1)&0x8000,"held input released")
            time.sleep(.25)
            assert text()==before and host.state(eid)["status"]=="cancelled"
            terminal=[e["payload"] for e in host.events if e["name"]=="script_state_changed" and
                      e["payload"].get("execution_id")==eid and e["payload"].get("status") in ("cancelled","completed","failed")]
            assert len(terminal)==1,terminal
            observed[case]=dict(execution=eid,barrier=barrier,pre_state=state,final=final,
                                terminal_events=terminal,elapsed_to_verified_ms=(time.perf_counter()-cutoff)*1000)
            print(case,eid,flush=True)
        proof=start("wait(10)")
        host.until(proof,"running")
        chord(hwnd,0x79)
        host.until(proof,"cancelled")
        for label,body in [("wait","wait(12)"),("busy","while true do end")]:
            eid=start('key_down("f6")\nlog("entered '+label+'")\n'+body+'\ntype_text("late",0)')
            mark=wait(lambda:marker(eid,"entered "+label),label)
            assert user.GetAsyncKeyState(0x75)&0x8000
            stop(label,eid,mark)
        sub=host.save('key_down("f6")\nlog("inside child")\nwait(12)\ntype_text("late",0)')
        eid=start('call_script("'+sub+'")\ntype_text("parent-late",0)')
        mark=wait(lambda:marker(eid,"inside child"),"child active")
        stop("child",eid,mark)
        eid=start('key_down("f6")\nlog("entering OCR")\nlocal b=ocr(nil,nil,"korean_PP-OCRv5_mobile_rec")\nlog("OCR finished")\ntype_text("late",0)')
        history = output / "data/history"
        app = Path(sys.executable).resolve().parent
        portable_manifest = app / "ScreenTrigger.portable.json"
        if portable_manifest.is_file():
            layout = json.loads(portable_manifest.read_bytes())
            if (layout.get("format"), layout.get("mode"), layout.get("data")) == (1, "portable", "data"):
                history = app / "data/history"
        def own_log(message):
            for path in history.glob(eid+"*.jsonl"):
                for line in path.read_text("utf8").splitlines():
                    if message in line:
                        return json.loads(line)
            return None
        # Waiting for a routine IPC query can itself outlast native inference.
        # Observe the private execution log and OS key directly before issuing Stop.
        mark=wait(lambda:own_log("entering OCR"),"start OCR")
        time.sleep(.12)
        assert not own_log("OCR finished") and user.GetAsyncKeyState(0x75)&0x8000
        stop("ocr",eid,mark,dict(status="running",observed_via="entered OCR, F6 held, no completion marker"))
        eid=start(f'log("drag")\ndrag({x},{y},{x+120},{y+30},3)\ntype_text("late",0)')
        wait(lambda:user.GetAsyncKeyState(1)&0x8000,"owned mouse down in drag")
        stop("drag",eid,dict(mouse_down=True,start=[x,y],end=[x+120,y+30]))
        eid=start('local n=2\nlog("at breakpoint")\ntype_text("late",0)',debug=True,line=3)
        mark=host.until(eid,"debug_paused")
        stop("breakpoint",eid,mark)
        frame=host.request("capture_window",binding_id=binding)["image"]
        target=host.request("create_target",draft=dict(name="no magenta",image_id=frame["image_id"],anchor_condition_id="c",
            conditions=dict(op="all",children=[dict(type="color",id="c",params=dict(color_space="rgb",
            color=dict(r=255,g=0,b=255),min_pixel_count=1000))])))["target"]["id"]
        host.request("release_image",image_id=frame["image_id"])
        eid=host.request("start_monitor",target_id=target,binding_id=binding,frequency_hz=10,
            wait_timeout_ms=200,on_timeout="retry",max_retries=2,retry_delay_ms=4000)["execution_id"]
        mark=wait(lambda:marker(eid,"监控等待超时"),"first attempt timed out before retry")
        stop("retry",eid,mark)
        time.sleep(.7)
        assert all(host.state(v["execution"])["status"]=="cancelled" for v in observed.values())
    finally:
        (output/"observations.json").write_text(json.dumps(observed,ensure_ascii=False,indent=2),"utf8")
        if host:
            host.close()
            (output/"events.json").write_text(json.dumps(host.events,ensure_ascii=False,indent=2),"utf8")
        if child.is_alive():
            pipe.send("quit")
            child.join(3)
        if child.is_alive():
            child.terminate()
            child.join(3)
        user.SetCursorPos(original.x,original.y)
        if previous and user.IsWindow(previous):user.SetForegroundWindow(previous)

def main(output):
    output.mkdir(exist_ok=False)
    trace=output/"host.log"
    with trace.open("xb") as log:
        command=[sys.executable,"-I","-B","-X","utf8",__file__,str(output.resolve()),"--worker"]
        status=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,env=dict(os.environ,ST_MEASURE_TIMING="1")).returncode
    if status:
        print(trace.read_text("utf8",errors="replace")[-2000:]);return status
    observed=json.loads((output/"observations.json").read_bytes())
    stamps={}
    for name,eid,ts in re.findall(r"\[gate-timing\] (\w+) ([\w-]+) (\d+)",trace.read_text("utf8",errors="replace")):
        stamps.setdefault(eid,{}).setdefault(name,[]).append(int(ts))
    result={}
    for case,obs in observed.items():
        times=stamps[obs["execution"]]
        start=times["stop_received"][0]
        closed=next(x for x in times["closed"] if x>=start)
        result[case]=dict(gate_ms=(closed-start)/1e6,dispatches_after=sum(x>closed for x in times.get("submitted",[])))
    assert len(result)==7 and all(r["dispatches_after"]==0 for r in result.values()),result
    (output/"summary.json").write_text(json.dumps(result,indent=2),"utf8")
    print(json.dumps(result));return 0

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output",type=Path)
    parser.add_argument("--worker",action="store_true")
    args=parser.parse_args()
    if args.worker:exercise(args.output)
    else:raise SystemExit(main(args.output))
