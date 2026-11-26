"""Check actual target-painted frames against two separate, owned Win32 windows."""
import argparse
import base64
import ctypes
from ctypes import wintypes as wt
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from PIL import Image, ImageDraw, ImageGrab, ImageWin
import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
from common import MiniWindow, gdi32, set_per_monitor_v2, user32
from verify_p1_runtime import Host, Fault, wait

X, Y, W, H = 150, 180, 230, 110
COVER = (213, 91, 46)


def frame(n):
    image = Image.new("RGB", (W, H), (n % 251, n // 251, 81))
    ImageDraw.Draw(image).text((36, 34), f"FRAME {n:05d}", fill="white")
    return image


def observer_window(pipe, title):
    set_per_monitor_v2()
    user32.GetDC.argtypes, user32.GetDC.restype = [wt.HWND], wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    observer = MiniWindow(title+" observer",X+W+10,Y,W,H,popup=True)
    cover = ImageWin.Dib(Image.new("RGB",(W,H),COVER))
    try:
        user32.SetWindowPos(observer.hwnd,-1,X+W+10,Y,W,H,0x40)
        user32.SetForegroundWindow(observer.hwnd)
        pipe.send(observer.hwnd)
        while True:
            observer.pump(10)
            if pipe.poll():
                op = pipe.recv()
                if op == "quit":
                    break
                if op == "cover":
                    user32.SetWindowPos(observer.hwnd,-1,X,Y,W,H,0x40)
                    user32.SetForegroundWindow(observer.hwnd)
                pipe.send(True)
            dc = user32.GetDC(observer.hwnd)
            try:
                cover.draw(ImageWin.HDC(dc),(0,0,W,H))
                gdi32.GdiFlush()
            finally:
                user32.ReleaseDC(observer.hwnd,dc)
    finally:
        observer.destroy()


def windows(pipe, out, target_title):
    set_per_monitor_v2()
    user32.GetDC.argtypes, user32.GetDC.restype = [wt.HWND], wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    n = 0
    def draw(image, dc):
        ImageWin.Dib(image).draw(ImageWin.HDC(dc), (0, 0, W, H))
        gdi32.GdiFlush()
    class Target(MiniWindow):
        def _wndproc(self, hwnd, msg, wp, lp):
            if msg in (0x317, 0x318):
                draw(frame(n),wp)
                return 1
            return super()._wndproc(hwnd,msg,wp,lp)
    target = Target(target_title,X,Y,W,H,popup=True)
    pipe.send(target.hwnd)
    try:
        while True:
            target.pump(10)
            if pipe.poll():
                op = pipe.recv()
                if op == "quit": break
                if op == "minimize": user32.ShowWindow(target.hwnd,6)
                elif op == "restore":
                    user32.ShowWindow(target.hwnd,4)
                elif op == "freeze":
                    pipe.send(True)
                    time.sleep(3.5)
                    continue
                elif op in ("protect", "unprotect"):
                    user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
                    pipe.send(bool(user32.SetWindowDisplayAffinity(target.hwnd, 0x11 if op == "protect" else 0)))
                    continue
                elif op == "close":
                    target.destroy()
                elif op == "replace":
                    target = Target(target_title, X,Y,W,H,popup=True)
                pipe.send(True)
            n = int(time.monotonic()*10) % 60000
            if target.hwnd and not user32.IsIconic(target.hwnd):
                dc = user32.GetDC(target.hwnd)
                try:
                    current=frame(n)
                    draw(current,dc)
                    with (out/"painted.jsonl").open("a",encoding="utf8") as log:
                        print(json.dumps(dict(number=n,time=time.monotonic(),
                            sha256=hashlib.sha256(current.tobytes("raw","BGR")).hexdigest())),file=log)
                finally: user32.ReleaseDC(target.hwnd,dc)
    finally:
        target.destroy()


def run(out, dll):
    out.mkdir(parents=True, exist_ok=False)
    set_per_monitor_v2()
    prev = user32.GetForegroundWindow()
    cursor = wt.POINT()
    user32.GetCursorPos(ctypes.byref(cursor))
    parent, child = multiprocessing.Pipe()
    observer_parent, observer_child = multiprocessing.Pipe()
    title = "ScreenTrigger capture "+uuid4().hex
    owner = multiprocessing.Process(target=windows,args=(child,out,title))
    owner.start()
    watch = None
    host = None
    def fixture(op):
        channel = observer_parent if op == "cover" else parent
        channel.send(op)
        assert channel.poll(8) and channel.recv()
    def record(step, **data):
        with (out/"result.jsonl").open("a",encoding="utf8") as log:
            print(json.dumps(dict(step=step,monotonic=time.monotonic(),**data),ensure_ascii=False),file=log)
    def denied(code, cmd, **params):
        try: host.request(cmd,**params)
        except Fault as exc:
            record(cmd,expected=code,error=exc.code)
            assert exc.code == code, exc
            return
        raise AssertionError(f"{cmd} unexpectedly returned an image")
    try:
        assert parent.poll(10)
        target = parent.recv()
        watch = multiprocessing.Process(target=observer_window,args=(observer_child,title))
        watch.start()
        assert observer_parent.poll(10)
        observer = observer_parent.recv()
        assert watch.pid != owner.pid
        record("independent window processes", target=target, target_pid=owner.pid, observer=observer, observer_pid=watch.pid)
        host = Host(dll,out)
        host.session = host.call("start")["session_id"]
        w = next(w for w in host.request("list_windows")["windows"] if w["title"] == title)
        assert w["pid"] == owner.pid and int(w["handle"],16)==target
        binding = host.request("bind_window",window_id=w["window_id"])["binding"]["binding_id"]
        scope = dict(binding_id=binding,window_mode="background")
        def caps():
            return host.request("get_window_capture_capabilities", binding_id=binding)["capture"]
        initial = caps()
        assert set(initial.values()) == {"unknown"}
        record("initial capture capabilities", actual=initial, target=target, pid=owner.pid)
        display = host.request("get_display_list")["displays"][0]["id"]
        script = host.save("wait(12)")
        denied("INVALID_PARAMS","execute_script",script_id=script,display_id=display,window_mode="background")
        if user32.GetForegroundWindow()!=observer:
            # Activate only after a hit test on the separately owned observer.
            pt=wt.POINT(X+W+25,Y+12)
            user32.WindowFromPoint.restype, user32.WindowFromPoint.argtypes = wt.HWND,[wt.POINT]
            assert user32.WindowFromPoint(pt)==observer
            user32.SetCursorPos(pt.x,pt.y)
            assert not user32.GetAsyncKeyState(1)&0x8000
            user32.mouse_event(2,0,0,0,0)
            user32.mouse_event(4,0,0,0,0)
        wait(lambda:user32.GetForegroundWindow()==observer,"owned observer takes focus")
        held = wt.POINT()
        user32.GetCursorPos(ctypes.byref(held))
        def shot(label,index):
            assert user32.GetForegroundWindow()==observer
            reply=host.request("capture_window",**scope)
            ref=reply["image"]
            preview=host.request("get_image_preview",image_id=ref["image_id"],max_edge=W)
            raw=base64.b64decode(preview["base64"],validate=True)
            (out/f"{label}-{index}.png").write_bytes(raw)
            image=Image.open(io.BytesIO(raw)).convert("RGB")
            red,green,blue=image.getpixel((6,6))
            number=green*251+red
            assert blue==81 and image.tobytes()==frame(number).tobytes(),(number,(red,green,blue))
            assert hashlib.sha256(image.tobytes("raw","BGR")).hexdigest()==ref["sha256"]
            paints = [json.loads(s) for s in (out/"painted.jsonl").read_text().splitlines()]
            assert any(p["number"]==number and p["sha256"]==ref["sha256"] for p in paints)
            assert user32.GetForegroundWindow()==observer
            current=wt.POINT()
            user32.GetCursorPos(ctypes.byref(current))
            assert (current.x,current.y)==(held.x,held.y)
            host.request("release_image",image_id=ref["image_id"])
            record(label,seq=number,frame_id=reply["frame_id"],image=ref,foreground=observer,cursor=[held.x,held.y])
            return number
        for label in ("foreground-switched","fully-covered"):
            if label=="fully-covered":
                fixture("cover")
                cover=Image.new("RGB",(W,H),COVER).tobytes()
                wait(lambda:ImageGrab.grab(bbox=(X,Y,X+W,Y+H)).convert("RGB").tobytes()==cover,"fully opaque cover")
                ImageGrab.grab(bbox=(X,Y,X+W,Y+H)).save(out/"actual-screen-covered.png")
            observed=[]
            for i in range(4):
                observed.append(shot(label,i))
                time.sleep(.12)
            assert len(set(observed))==4
            state=caps()
            record("capture capabilities",scene=label,actual=state)
            assert state["non_foreground"]=="available"
            assert state["fully_occluded"]==("available" if label=="fully-covered" else "unknown")
        denied("WINDOW_UNAVAILABLE","capture_window",binding_id=binding)
        root=host.request("execute_script",script_id=script,**scope)["execution_id"]
        host.until(root,"running")
        fixture("minimize")
        record("minimized", actual=host.until(root,"paused"), capabilities=caps())
        denied("WINDOW_UNAVAILABLE","capture_window",**scope)
        fixture("restore")
        assert host.state(root)["status"]=="paused"
        host.request("resume_script",execution_id=root)
        record("manually resumed", actual=host.until(root,"running"))
        fixture("freeze")
        denied("CAPTURE_FAILED","capture_window",**scope)
        fixture("thaw")
        shot("recovered",0)
        fixture("protect")
        denied("CAPABILITY_DENIED", "capture_window", **scope)
        protected = host.request("get_window_capture_capabilities",binding_id=binding)
        record("protected", actual=protected)
        assert set(protected["capture"].values()) == {"unavailable"} and protected["reason"]
        fixture("unprotect")
        shot("after-protection",0)
        sampling = host.save("capture_screen(); return 36")
        children = lambda: {child.pid for child in psutil.Process().children(recursive=True)}
        idle = children()
        fixture("freeze")
        second = host.request("execute_script",script_id=sampling,**scope)["execution_id"]
        fresh = wait(lambda: children() - idle,"pending target paint process",seconds=2)
        host.request("stop_script",execution_id=second)
        terminal = host.until(second,"cancelled")
        wait(lambda: not children().intersection(fresh),"cancelled paint child reaped",seconds=2)
        fixture("thaw")
        assert host.state(second)["status"] == "cancelled"
        record("stopped while paint was pending", actual=terminal, child_pids=sorted(fresh))
        fixture("close")
        stopped=host.until(root,"failed")
        assert stopped["error"]["code"]=="WINDOW_NOT_FOUND"
        record("closed", actual=stopped)
        denied("WINDOW_NOT_FOUND","capture_window",**scope)
        fixture("replace")
        denied("WINDOW_NOT_FOUND","capture_window",**scope)
        record("completed",actual=True)
    finally:
        if host: host.close()
        if owner.is_alive():
            parent.send("quit"); owner.join(8)
        if owner.is_alive(): owner.kill(); owner.join()
        if watch is not None and watch.is_alive():
            observer_parent.send("quit"); watch.join(8)
        if watch is not None and watch.is_alive():
            watch.kill(); watch.join()
        user32.SetCursorPos(cursor.x,cursor.y)
        if prev and user32.IsWindow(prev): user32.SetForegroundWindow(prev)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("output",type=Path)
    p.add_argument("library",type=Path)
    a=p.parse_args()
    run(a.output.resolve(),a.library.resolve(strict=True))
