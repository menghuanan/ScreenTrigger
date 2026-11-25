"""Drive the product overlay only while it owns both foreground and drag endpoints."""
import ctypes
import hashlib
from ctypes import wintypes as wt
import json
from pathlib import Path
import sys
import subprocess

from common import enumerate_monitors, set_per_monitor_v2, user32
from verify_selection import TimerTarget, drag, invoke, own_window, require


def verify_assets(folder):
    from PIL import Image
    data = (folder/"data").resolve()
    current = json.loads((data/"catalog/current.json").read_bytes())
    generation = data/"catalog/generations"/current["generation"]
    targets = json.loads((generation/"targets.json").read_bytes())["targets"]
    groups = json.loads((generation/"groups.json").read_bytes())["groups"]
    assert [t["sample_no"] for t in sorted(targets,key=lambda t:t["sample_no"])] == [1,2]
    captured, imported = sorted(targets,key=lambda t:t["sample_no"])
    assert imported["name"] == "P1 renamed" and imported["group_id"] in {
        g["id"] for g in groups if g["name"] == "P1 flow group"}
    def asset(target):
        path = (data/target["image"]["path"]).resolve()
        assert path.is_relative_to(data/"images")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == target["image"]["sha256"]
        return path
    reference = json.loads((folder/"selection-reference.json").read_bytes())
    colors = {tuple(frame["rgb"]) for frame in reference["frames"]}
    with Image.open(asset(captured)) as source:
        assert source.size == tuple(reference["size"])
        pixels = source.convert("RGB").getcolors()
        assert len(pixels)==1 and pixels[0][1] in colors, pixels
    assert asset(imported).read_bytes() == (folder/"fixture.png").read_bytes()
    print(json.dumps(dict(generation=current["generation"], captured_pixels=pixels,
                          original_sha256=imported["image"]["sha256"], group=imported["group_id"])))


def main():
    pid, main_hwnd, folder = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    set_per_monitor_v2()
    monitor = next(m for m in enumerate_monitors() if m["primary"])
    target = TimerTarget(monitor["physical_rect"])
    cursor = wt.POINT()
    user32.GetCursorPos(ctypes.byref(cursor))
    try:
        user32.SetForegroundWindow(main_hwnd)
        invoke(main_hwnd, "StartSelection")
        find_overlay = lambda: own_window(pid, "ScreenTrigger · 选择区域")
        overlay = target.wait(find_overlay, 10)
        target.wait(lambda:user32.GetForegroundWindow() == overlay, 5)
        def read_controls():
            command = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]{int(overlay)})
$root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition) |
    ForEach-Object {{ [ordered]@{{id=$_.Current.AutomationId;name=$_.Current.Name;enabled=$_.Current.IsEnabled}} }} |
    ConvertTo-Json -Compress
"""
            value = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                             "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + command],
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            return json.loads(value) if value.strip() else []
        target.wait(lambda: any(c["id"] == "CancelSelection" and c["enabled"] for c in read_controls()), 10)
        x, y = target.region[:2]
        a, b = (x+12,y+26), (x+76,y+74)
        drag(overlay, b, a, target)
        controls = read_controls()
        (folder/"overlay-after-drag.json").write_text(json.dumps(controls, ensure_ascii=False), encoding="utf8")
        target.wait(lambda:len(target.frames) >= 5, 3)
        def confirm_when_enabled():
            try:
                invoke(overlay, "ConfirmSelection")
                return True
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 2:
                    raise
                return False
        target.wait(confirm_when_enabled, 10)
        target.wait(lambda: not find_overlay() and user32.IsWindowVisible(main_hwnd), 8)
        final = {"size":[64,48], "origin":a, "frames":target.frames, "foreground_restored":user32.GetForegroundWindow()==main_hwnd}
        (folder/"selection-reference.json").write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding="utf8")
        require(final["foreground_restored"], "main window did not regain foreground")
        print("Selection returned to product window", flush=True)
    finally:
        user32.SetCursorPos(cursor.x,cursor.y)
        target.destroy()


if __name__=="__main__":
    if len(sys.argv)==3 and sys.argv[1]=="--verify-assets":
        verify_assets(Path(sys.argv[2]))
    else:
        main()
