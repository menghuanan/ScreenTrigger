"""Verify receiver-side effects of saved Lua against two owned, independently pumped Win32 windows."""
import argparse
import base64
import ctypes as ct
from ctypes import wintypes as wt
import io
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4

from PIL import Image, ImageGrab, ImageWin

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
import common
from input_probe import unicode_char_events
from verify_p1_runtime import Fault, Host, wait
from verify_selection import send

user = common.user32
X, Y, W, H = 210, 190, 300, 160
EX, EY, EW, EH = 70, 45, 175, 72
CAP = user.RegisterWindowMessageW("ScreenTrigger.BackgroundInput.Capabilities.v1")
INPUT_MSG = {0x100, 0x101, 0x102, 0x104, 0x105, 0x200, 0x201, 0x202, 0x20a}
FRONT_COLOUR = (207, 78, 23)
FUNCTIONS = {"click", "drag", "scroll", "key", "hotkey"}
SUBCLASS = ct.WINFUNCTYPE(ct.c_ssize_t, wt.HWND, wt.UINT, ct.c_size_t, ct.c_ssize_t,
                         ct.c_size_t, ct.c_size_t)
cc = ct.WinDLL("comctl32")
cc.SetWindowSubclass.argtypes = [wt.HWND, SUBCLASS, ct.c_size_t, ct.c_size_t]
cc.DefSubclassProc.argtypes = [wt.HWND, wt.UINT, ct.c_size_t, ct.c_ssize_t]
cc.DefSubclassProc.restype = ct.c_ssize_t
user.GetDC.argtypes, user.GetDC.restype = [wt.HWND], wt.HDC
user.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user.GetFocus.restype = wt.HWND


def mouse_point(lp):
    return [ct.c_short(lp & 65535).value, ct.c_short((lp >> 16) & 65535).value]


def cursor():
    point = wt.POINT()
    assert user.GetCursorPos(ct.byref(point))
    return [point.x, point.y]


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf8")


def fixture(channel, title, role):
    common.set_per_monitor_v2()
    events = []
    mode = {"mask": 31, "delay": False, "seq": 0}
    def picture():
        n = mode["seq"]
        return Image.new("RGB", (W,H), FRONT_COLOUR if role=="front" else (n%251,n//251,81))
    def paint(dc):
        ImageWin.Dib(picture()).draw(ImageWin.HDC(dc), (0,0,W,H))
        common.gdi32.GdiFlush()
    def event(hwnd,msg,wp,lp,where):
        if msg not in INPUT_MSG:
            return
        row={"t":time.monotonic(),"hwnd":hwnd,"where":where,"message":msg}
        if msg >= 0x200:
            row.update(point=mouse_point(lp), flags=wp & 65535)
            if msg==0x20a:row["delta"]=ct.c_short((wp>>16)&65535).value
        else:
            row.update(vk=wp, key_flags=(lp>>16)&65535)
        events.append(row)
        if role=="target" and mode["delay"] and msg==0x100 and wp==0x79:
            time.sleep(.40)
    class Root(common.MiniWindow):
        def _wndproc(self,hwnd,msg,wp,lp):
            if msg==CAP:
                return 0x53540000|mode["mask"] if role=="target" and mode["mask"] is not None and not (wp or lp) else 0
            if msg in (0x317,0x318):
                paint(wp)
                return 1
            event(hwnd,msg,wp,lp,"root")
            if role=="target" and msg in INPUT_MSG:
                return 0
            return super()._wndproc(hwnd,msg,wp,lp)
    def create():
        initial = X+W+50 if role=="front" else X
        window=Root(title,initial,Y,W,H,popup=True)
        assert window.hwnd
        edit=common.create_edit_child(window.hwnd,"",EX,EY,EW,EH)
        assert edit
        @SUBCLASS
        def receive(hwnd,msg,wp,lp,ident,ref):
            event(hwnd,msg,wp,lp,"child")
            if role=="target" and msg in INPUT_MSG:
                return 0
            return cc.DefSubclassProc(hwnd,msg,wp,lp)
        assert cc.SetWindowSubclass(edit,receive,1,0)
        return window, edit, receive
    w, edit, proc=create()
    channel.send((w.hwnd,edit))
    try:
        while True:
            w.pump(8)
            if channel.poll():
                cmd,*args=channel.recv()
                response=True
                if cmd=="quit":break
                if cmd=="events":response=events.copy()
                elif cmd=="view":response={"text":common.edit_get_text(edit),"focus":user.GetFocus(),
                                        "seq":mode["seq"],"root":w.hwnd,"edit":edit}
                elif cmd=="focus":
                    assert role=="front"
                    user.SetForegroundWindow(w.hwnd);user.SetFocus(edit)
                elif cmd=="side" or cmd=="cover":
                    assert role=="front"
                    user.SetWindowPos(w.hwnd,-1,X if cmd=="cover" else X+W+50,Y,W,H,0x50)
                elif cmd=="mask":mode["mask"]=args[0]
                elif cmd=="delay":mode["delay"]=bool(args[0])
                elif cmd=="minimize":user.ShowWindow(w.hwnd,6)
                elif cmd=="restore":user.ShowWindow(w.hwnd,4)
                elif cmd=="close":w.destroy()
                elif cmd=="replace":
                    assert w.hwnd is None
                    w,edit,proc=create()
                    response=(w.hwnd,edit)
                channel.send(response)
            if w.hwnd and not user.IsIconic(w.hwnd):
                mode["seq"]=int(time.monotonic()*13)%60000
                dc=user.GetDC(w.hwnd)
                if dc:
                    try:paint(dc)
                    finally:user.ReleaseDC(w.hwnd,dc)
    finally:
        w.destroy()


def exercise(out,dll):
    out.mkdir(parents=True,exist_ok=False)
    common.set_per_monitor_v2()
    original_front=user.GetForegroundWindow()
    original_cursor=cursor()
    title="ScreenTrigger background actions "+uuid4().hex
    processes=[]; peers={}
    host=None
    tracking=threading.Event(); samples=[]
    def watch():
        while not tracking.wait(.01):
            samples.append([time.monotonic(),user.GetForegroundWindow(),*cursor()])
    observer=None
    def record(case,**data):
        with (out/"observations.jsonl").open("a",encoding="utf8") as stream:
            print(json.dumps({"case":case,"time":time.monotonic(),**data},ensure_ascii=False),file=stream)
    def rpc(role,cmd,*args):
        conn=peers[role]
        conn.send((cmd,*args))
        assert conn.poll(7),(role,cmd)
        return conn.recv()
    def state(script,expected="completed"):
        eid=host.request("execute_script",script_id=host.save(script),**scope)["execution_id"]
        result=host.until(eid,expected)
        record("execution terminal",source=script,actual=result,binding_id=binding,session_id=host.session)
        return result
    def caps():
        reply=host.request("get_window_input_capabilities",binding_id=binding)
        record("input capabilities",actual=reply)
        assert reply["binding_id"]==binding and set(reply["input"])==FUNCTIONS
        return reply["input"]
    try:
        host=Host(dll,out)
        host.session=host.call("start")["session_id"]
        handles={}
        for role in ("target","front"):
            local,remote=mp.Pipe()
            proc=mp.Process(target=fixture,args=(remote,title+role,role))
            proc.start();processes.append(proc);peers[role]=local
            assert local.poll(8)
            handles[role]=local.recv()
        target,child=handles["target"]
        front,front_edit=handles["front"]
        row=next(r for r in host.request("list_windows")["windows"] if r["title"]==title+"target")
        assert row["pid"]==processes[0].pid and int(row["handle"],16)==target
        binding=host.request("bind_window",window_id=row["window_id"])["binding"]["binding_id"]
        scope={"binding_id":binding,"window_mode":"background"}
        rpc("front","side");rpc("front","focus")
        pos=(X+W+50+EX+10,Y+EY+10)
        assert user.GetAncestor(user.WindowFromPoint(wt.POINT(*pos)),2)==front
        assert user.SetCursorPos(*pos)
        if user.GetForegroundWindow()!=front or rpc("front","view")["focus"]!=front_edit:
            assert user.WindowFromPoint(wt.POINT(*pos))==front_edit and not user.GetAsyncKeyState(1)&0x8000
            user.mouse_event(2,0,0,0,0);user.mouse_event(4,0,0,0,0)
        wait(lambda:user.GetForegroundWindow()==front and rpc("front","view")["focus"]==front_edit,"private observer focus")
        held=cursor()
        record("environment",fixture_pids=[p.pid for p in processes],handles=handles,host_session=host.session,
               binding=binding,window=row,display=host.request("get_display_list"),cursor=held)
        assert set(caps().values())=={"available"}
        observer=threading.Thread(target=watch,daemon=True);observer.start()
        def fg_key(char):
            assert user.GetForegroundWindow()==front and rpc("front","view")["focus"]==front_edit
            assert not any(user.GetAsyncKeyState(k)&0x8000 for k in (0x10,0x11,0x12,ord(char.upper())))
            send(unicode_char_events(char))
        def shot(label):
            assert user.GetForegroundWindow()==front
            image=host.request("capture_window",**scope)
            image_id=image["image"]["image_id"]
            data=host.request("get_image_preview",image_id=image_id,max_edge=W)
            blob=base64.b64decode(data["base64"],validate=True)
            (out/(label+".png")).write_bytes(blob)
            pixel=Image.open(io.BytesIO(blob)).convert("RGB").getpixel((8,8))
            host.request("release_image",image_id=image_id)
            assert pixel[2]==81 and pixel!=FRONT_COLOUR
            return pixel[1]*251+pixel[0]
        def pattern(events):
            return [(r["message"],r.get("vk")) for r in events if r["message"] in (0x100,0x101,0x104,0x105)]
        for scene in ("side","cover"):
            rpc("front",scene)
            wait(lambda:user.GetForegroundWindow()==front,"observer remains foreground")
            expected_root=front if scene=="cover" else target
            for px,py in [(X+4,Y+4),(X+W-4,Y+4),(X+4,Y+H-4),(X+W-4,Y+H-4)]:
                assert user.GetAncestor(user.WindowFromPoint(wt.POINT(px,py)),2)==expected_root
            if scene=="cover":
                captured=ImageGrab.grab(bbox=(X+5,Y+5,X+25,Y+25)).convert("RGB")
                assert captured.getextrema()==tuple((v,v) for v in FRONT_COLOUR)
                captured.save(out/"physical-cover.png")
            seq0=shot(scene+"-before")
            for where,offset in (("root",(12,14)),("child",(EX+13,EY+12))):
                dx,dy=offset;a=(X+dx,Y+dy);b=(a[0]+20,a[1]+12)
                ov_before=rpc("front","view")["text"]
                mark=len(rpc("target","events"));original=rpc("front","events")
                start_sample=len(samples)
                script=(f"wait(.15);click_at({a[0]},{a[1]});drag({a[0]},{a[1]},{b[0]},{b[1]},.12);"
                        f"move_to({b[0]},{b[1]},0);scroll(1);press_key('f8');hotkey('ctrl','f7');wait(.1);return 37")
                eid=host.request("execute_script",script_id=host.save(script),**scope)["execution_id"]
                host.until(eid,"running")
                fg_key("v")
                terminal=host.until(eid,"completed")
                assert terminal["result"]==37,terminal
                received=rpc("target","events")[mark:]
                front_events=rpc("front","events")[len(original):]
                view=rpc("front","view")
                track=samples[start_sample:]
                record("five delivered actions",scene=scene,receiver=where,binding=binding,execution=eid,source=script,
                       expected_screen=[a,b],actual=received,foreground_events=front_events,foreground_text=view["text"],
                       focus=view["focus"],foreground=front,cursor=held,sampled_geometry=track)
                assert track and all(r[1]==front and r[2:]==held for r in track)
                assert view["focus"]==front_edit and view["text"]==ov_before+"v"
                assert pattern(front_events)==[(0x100,0xe7),(0x101,0xe7)],front_events
                assert [r["vk"] for r in front_events if r["message"]==0x102]==[ord("v")],front_events
                assert all(r["message"]==0x200 or r["message"]<0x200 for r in front_events)
                pbase=(X+(EX if where=="child" else 0),Y+(EY if where=="child" else 0))
                local_a=[a[0]-pbase[0],a[1]-pbase[1]];local_b=[b[0]-pbase[0],b[1]-pbase[1]]
                action=[r for r in received if r["message"] in (0x201,0x202,0x20a)]
                assert [(r["where"],r["message"],r["point"]) for r in action]==[
                    (where,0x201,local_a),(where,0x202,local_a),
                    (where,0x201,local_a),(where,0x202,local_b),(where,0x20a,list(b))],action
                assert action[-1]["delta"]==120
                dragged=[r for r in received if r["message"]==0x200 and r["flags"]&1]
                assert dragged and all(r["where"]==where for r in dragged)
                assert pattern(received)==[(0x100,0x77),(0x101,0x77),(0x100,0x11),(0x100,0x76),(0x101,0x76),(0x101,0x11)]
                assert all(r["where"]==where and r["hwnd"]==(child if where=="child" else target) for r in received if r["message"]<0x200)
                assert all(r["hwnd"] in (target,child) for r in received)
            seq1=shot(scene+"-after")
            assert seq0!=seq1
            record("dynamic target frame",scene=scene,sequences=[seq0,seq1],current_focus=rpc("front","view"))
        mark=len(rpc("target","events"))
        hold=host.save("key_down('ctrl');key_down('f8');wait(20);press_key('f7')")
        eid=host.request("execute_script",script_id=hold,**scope)["execution_id"]
        wait(lambda:pattern(rpc("target","events")[mark:])==[(0x100,0x11),(0x100,0x77)],"target received held keys")
        stopped_at=time.monotonic();host.request("stop_script",execution_id=eid)
        terminal=host.until(eid,"cancelled")
        wait(lambda:len(pattern(rpc("target","events")[mark:]))>=4,"release after stop")
        time.sleep(.25);after=rpc("target","events")[mark:]
        record("stop releases only held keys",stopped_at=stopped_at,received=after,actual=terminal)
        assert pattern(after)==[(0x100,0x11),(0x100,0x77),(0x101,0x77),(0x101,0x11)]
        # Previous state is measured while foreground and target are unchanged. No physical target input is possible.
        assert user.GetForegroundWindow()==front and cursor()==held
        minority=len(rpc("target","events"))
        eid=host.request("execute_script",script_id=host.save("key_down('f8');wait(.9);press_key('f7');return 37"),**scope)["execution_id"]
        wait(lambda:pattern(rpc("target","events")[minority:])==[(0x100,0x77)],"key before minimizing")
        rpc("target","minimize");paused=host.until(eid,"paused")
        record("minimize observed",paused=paused,messages=rpc("target","events")[minority:])
        wait(lambda:(0x101,0x77) in pattern(rpc("target","events")[minority:]),"release held background key on minimize")
        assert set(caps().values())=={"unavailable"}
        rpc("target","restore");time.sleep(.30)
        after=rpc("target","events")[minority:]
        assert host.state(eid)["status"]=="paused" and pattern(after)==[(0x100,0x77),(0x101,0x77)]
        assert shot("restored")>=0
        host.request("resume_script",execution_id=eid);resumed=host.until(eid,"completed")
        assert (0x100,0x76) in pattern(rpc("target","events")[minority:])
        record("explicit resume after minimize and fresh frame",paused=paused,restored_events=after,completed=resumed)
        rpc("target","mask",None)
        assert set(caps().values())=={"unknown"}
        mark=len(rpc("target","events"))
        denied=state("press_key('f8')","failed")
        assert denied["error"]["code"]=="CAPABILITY_DENIED" and not rpc("target","events")[mark:]
        rpc("target","mask",31);assert set(caps().values())=={"available"}
        rpc("target","delay",True);mark=len(rpc("target","events"))
        timed=state("press_key('f10')","failed");time.sleep(.55)
        received=rpc("target","events")[mark:]
        record("unconfirmed delivery",result=timed,received=received)
        assert timed["error"]["code"]=="INPUT_FAILED" and timed["error"]["details"].get("delivery_unknown") is True
        assert pattern(received).count((0x100,0x79))==1
        rpc("target","delay",False)
        eid=host.request("execute_script",script_id=host.save("wait(3);press_key('f7')"),**scope)["execution_id"]
        host.until(eid,"running")
        mark=len(rpc("target","events"));rpc("target","close")
        terminal=host.until(eid,"failed")
        assert terminal["error"]["code"]=="WINDOW_NOT_FOUND"
        replacement=rpc("target","replace")
        try:host.request("get_window_input_capabilities",binding_id=binding)
        except Fault as exc:assert exc.code=="WINDOW_NOT_FOUND",exc
        else:raise AssertionError("closed binding accepted replacement")
        assert not pattern(rpc("target","events")[mark:])
        record("closed target and same-title replacement",old=target,new=replacement,result=terminal)
    finally:
        tracking.set()
        if observer:observer.join(2)
        if host:host.close()
        for conn in peers.values():
            try:conn.send(("quit",))
            except (EOFError,BrokenPipeError):pass
        for proc in processes:
            proc.join(4)
            if proc.is_alive():proc.terminate();proc.join()
        if original_front and user.IsWindow(original_front):user.SetForegroundWindow(original_front)
        user.SetCursorPos(*original_cursor)
        write_json(out/"foreground-samples.json",samples)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("output",type=Path);p.add_argument("library",type=Path)
    args=p.parse_args()
    exercise(args.output.resolve(),args.library.resolve(strict=True))
