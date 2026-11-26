"""Display a known image in an isolated borderless physical-pixel window during an integration test."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import hashlib
import json
import sys
import time
from PIL import Image, ImageGrab, ImageWin

PROBES=Path(__file__).resolve().parents[2]/"tools/probes/windows"
sys.path.insert(0,str(PROBES))
from common import GA_ROOT, MiniWindow, set_per_monitor_v2, user32, gdi32

def main():
    folder=Path(sys.argv[1])
    set_per_monitor_v2()
    x,y=100,150
    sequence=json.loads(Path(sys.argv[2]).read_bytes())["images"] if len(sys.argv)>2 else []
    index=0
    def source():
        if not sequence: return Image.open(folder/"fixture.png").convert("RGB")
        item=sequence[index]
        picture=Image.open(item["path"]).convert("RGB")
        if hashlib.sha256(picture.tobytes()).hexdigest()!=item["rgb_sha256"]:
            raise ValueError("Fixture pixels differ from their frozen hash")
        return picture
    img=source()
    w,h=img.size
    dib=ImageWin.Dib(img)
    win=MiniWindow("ScreenTrigger · release image fixture",x,y,w,h,popup=True)
    user32.WindowFromPoint.argtypes=[wintypes.POINT]
    user32.WindowFromPoint.restype=wintypes.HWND
    user32.GetDC.argtypes=[wintypes.HWND]
    user32.GetDC.restype=wintypes.HDC
    user32.ReleaseDC.argtypes=[wintypes.HWND,wintypes.HDC]
    def owns_area():
        return all(user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(a,b)),GA_ROOT)==win.hwnd
                   for a,b in [(x,y),(x+w-1,y+h-1),(x+w//2,y+h//2)])
    try:
        end=time.monotonic()+max(85,90*len(sequence))
        ready=folder/("shown-0.json" if sequence else "fixture-visible.json")
        while time.monotonic()<end and not (folder/"fixture-stop").exists():
            win.pump(30)
            if sequence and index+1<len(sequence) and (folder/f"request-{index+1}").exists():
                index+=1
                img=source()
                assert img.size==(w,h)
                dib=ImageWin.Dib(img)
                ready=folder/f"shown-{index}.json"
            user32.SetWindowPos(win.hwnd,-1,x,y,w,h,0x10)
            dc=user32.GetDC(win.hwnd)
            try:
                dib.draw(ImageWin.HDC(dc),(0,0,w,h))
                gdi32.GdiFlush()
            finally: user32.ReleaseDC(win.hwnd,dc)
            if not ready.exists() and owns_area():
                grabbed=ImageGrab.grab(bbox=(x,y,x+w,y+h)).convert("RGB")
                if grabbed.tobytes()==img.tobytes():
                    with ready.open("x",encoding="utf8") as f:
                        json.dump(dict(origin=[x,y],size=[w,h],hwnd=win.hwnd,
                                       rgb_sha256=hashlib.sha256(grabbed.tobytes()).hexdigest()),f)
            time.sleep(.04)
        if not ready.exists(): raise AssertionError("Visible pixels did not match the owned fixture")
    finally:win.destroy()

if __name__=="__main__":main()
