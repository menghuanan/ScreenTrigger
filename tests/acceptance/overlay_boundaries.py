"""Drive the installed product's overlay on the primary physical display."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys

PROBES = Path(__file__).resolve().parents[2] / "tools/probes/windows"
sys.path.insert(0, str(PROBES))
from common import enumerate_monitors, set_per_monitor_v2, user32
from input_probe import vk_event
from verify_selection import TimerTarget, drag, invoke, own_window, send


def main():
    pid, hwnd, output = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
    set_per_monitor_v2()
    monitor = next(m for m in enumerate_monitors() if m["primary"])
    fixture = TimerTarget(monitor["physical_rect"])
    cursor = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(cursor))
    observations = []
    def overlay():
        return own_window(pid, "ScreenTrigger · 选择区域")
    try:
        for scenario in ("escape", "close", "minimum"):
            user32.SetForegroundWindow(hwnd)
            invoke(hwnd, "StartSelection")
            win = fixture.wait(overlay, 10)
            fixture.wait(lambda: user32.GetForegroundWindow() == win and not user32.IsWindowVisible(hwnd))
            observation = dict(scenario=scenario, display=monitor, owned_overlay=int(win))
            if scenario == "escape":
                send([vk_event(27), vk_event(27, up=True)])
            elif scenario == "close":
                assert user32.PostMessageW(win, 0x10, 0, 0)
            else:
                # Wait for the controls to finish loading before drawing a new region.
                ready = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$ae=[Windows.Automation.AutomationElement]
$win=$ae::FromHandle([intptr]{int(win)})
$cond=[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'CancelSelection')
$control=$win.FindFirst([Windows.Automation.TreeScope]::Descendants,$cond)
if (!$control -or !$control.Current.IsEnabled) {{exit 2}}
"""
                def button_ready():
                    return subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",ready],
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=8).returncode == 0
                fixture.wait(button_ready, 12)
                x, y = fixture.region[:2]
                a = (x+25,y+29)
                drag(win, a, (a[0]+9,a[1]+10),fixture)
                try:
                    invoke(win, "ConfirmSelection")
                except subprocess.CalledProcessError as error:
                    assert error.returncode == 2
                else:
                    raise AssertionError("9 × 10 incorrectly enabled confirmation")
                assert overlay()==win
                drag(win, a, (a[0]+10,a[1]+10), fixture)
                def confirm():
                    try: invoke(win,"ConfirmSelection"); return True
                    except subprocess.CalledProcessError as error:
                        if error.returncode==2: return False
                        raise
                fixture.wait(confirm, 12)
                observation.update(physical_origin=a, rejected_size=[9,10], accepted_size=[10,10])
            fixture.wait(lambda:not overlay() and user32.IsWindowVisible(hwnd) and user32.GetForegroundWindow()==hwnd,10)
            observation["main_foreground_restored"] = True
            observations.append(observation)
    finally:
        (output/"overlay-branches.json").write_text(json.dumps(observations,ensure_ascii=False,indent=2),"utf8")
        fixture.destroy()
        user32.SetCursorPos(cursor.x,cursor.y)


if __name__=="__main__":
    main()
