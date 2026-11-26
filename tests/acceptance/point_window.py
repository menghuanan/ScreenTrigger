"""Exercise the actual WinUI point picker over a private HWND and collect its input queue."""
import ctypes
from ctypes import wintypes as wt
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import time

from anchor_window import receiver
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probes/windows"))
from common import GA_ROOT, base_environment, set_per_monitor_v2, user32
from verify_selection import invoke, own_window, require, send
from input_probe import vk_event

user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND


def wait(read, message, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := read():
            return result
        time.sleep(.04)
    raise TimeoutError(message)


def controls(hwnd, ids):
    # Limit the query to this process window and the named product fields.
    names = ",".join("'" + name + "'" for name in ids)
    code = f"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]{int(hwnd)})
$result=@{{}}
foreach ($id in @({names})) {{
  $condition=New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::AutomationIdProperty,$id)
  $item=$root.FindFirst([Windows.Automation.TreeScope]::Descendants,$condition)
  if ($item) {{
    $p=$null
    if ($item.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$p)) {{ $result[$id]=$p.Current.Value }}
    else {{ $result[$id]=$item.Current.Name }}
  }}
}}
$result | ConvertTo-Json -Compress"""
    data = subprocess.check_output(["powershell.exe","-NoProfile","-NonInteractive","-Command",code],
                                   timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(data)


def bind_receiver(main, pid, folder):
    code = r"""
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$a=[Windows.Automation.AutomationElement]
$tree=[Windows.Automation.TreeScope]::Descendants
$root=$a::FromHandle([IntPtr]MAIN)
function Find($id) { $root.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($a::AutomationIdProperty,$id))) }
function Nav($id) {
    $item=Find $id
    if (!$item -or $item.Current.IsOffscreen) {
        $more=Find TopNavOverflowButton
        if($more){$more.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()}
    }
    (Find $id).GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
}
Nav NavRun
$end=[DateTime]::UtcNow.AddSeconds(8)
do { $box=Find WindowPicker; $binding=Find WindowBinding; if($box -and $box.Current.IsEnabled -and $binding.Current.Name -match '绑定'){break}; Start-Sleep -Milliseconds 150 } while([DateTime]::UtcNow -lt $end)
$box.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
$end=[DateTime]::UtcNow.AddSeconds(6)
do {
    $items=$root.FindAll($tree,[Windows.Automation.Condition]::TrueCondition)
    $receiver=$items | Where-Object {$_.Current.Name -match 'ScreenTrigger private point receiver.*PID OWNER'} | Select-Object -First 1
    if($receiver){break}
    Start-Sleep -Milliseconds 150
} while([DateTime]::UtcNow -lt $end)
if(!$receiver){throw "Receiver not listed in expanded window catalog, count $($items.Count)"}
$receiver.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
$expand=$box.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
if ($expand.Current.ExpandCollapseState -eq [Windows.Automation.ExpandCollapseState]::Expanded) { $expand.Collapse() }
$end=[DateTime]::UtcNow.AddSeconds(6)
do {
    $bind=$root.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($a::NameProperty,'绑定窗口')))
    if($bind -and $bind.Current.IsEnabled){break}
    Start-Sleep -Milliseconds 100
} while([DateTime]::UtcNow -lt $end)
if(!$bind){throw 'Bind button missing after closing the window list'}
$bind.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
$end=[DateTime]::UtcNow.AddSeconds(6)
do {
    $label=(Find WindowBinding).Current.Name
    if($label -match '已绑定.*private point receiver'){break}
    Start-Sleep -Milliseconds 100
} while([DateTime]::UtcNow -lt $end)
if($label -notmatch '已绑定.*private point receiver'){throw 'private binding not confirmed'}
Nav NavScripts
"""
    with (folder/"binding.log").open("xb") as log:
        subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",code.replace("MAIN",str(main)).replace("OWNER",str(pid))],
                       check=True, timeout=24, stdout=log,stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)


def run(pid, main, folder):
    awareness = set_per_monitor_v2()
    local, remote = multiprocessing.Pipe()
    child = multiprocessing.Process(target=receiver, args=(remote,"ScreenTrigger private point receiver"))
    child.start()
    require(local.poll(8), "private receiver unavailable")
    target = local.recv()
    cursor = wt.POINT()
    user32.GetCursorPos(ctypes.byref(cursor))
    events = []
    report = {"environment":base_environment(awareness),"target":int(target),"owner_pid":child.pid,"steps":events}
    title = "ScreenTrigger · 取点"
    fields = ["DraftPointX","DraftPointY","ScriptFeedback","ScriptSource"]
    point, after = (300,400), (309,405)
    def received():
        local.send("events")
        require(local.poll(3),"private receiver did not answer")
        return local.recv()
    def own(overlay, pos):
        hit = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(*pos)), GA_ROOT)
        # The nonactivating mouse window belongs exclusively to the visible picker.
        return user32.GetForegroundWindow() == overlay and (hit == overlay or (hit and user32.GetWindow(hit, 4) == overlay))
    def start():
        previous = controls(main,fields)
        invoke(main,"StartPointPick")
        overlay=wait(lambda: own_window(pid,title),"overlay missing")
        wait(lambda: own(overlay,point),"overlay does not own the selected point")
        return previous, overlay
    try:
        local.send("move")
        require(local.poll(3),"receiver was not positioned")
        local.recv()
        before = received()
        previous, overlay = start()
        require(previous["DraftPointX"]=="19" and previous["DraftPointY"]=="-4","original draft missing")
        user32.SetCursorPos(*point)
        def readout():
            value=controls(overlay,["PointReadout"]).get("PointReadout","")
            return value if f"X {point[0]}" in value and f"Y {point[1]}" in value else None
        text=wait(readout,"physical point readout")
        require(own(overlay,point) and not (user32.GetAsyncKeyState(1)&0x8000),"unsafe press")
        user32.mouse_event(2,0,0,0,0)
        try:
            time.sleep(.05)
            require(own(overlay,after),"unsafe release")
            user32.SetCursorPos(*after)
            time.sleep(.04)
        finally:
            user32.mouse_event(4,0,0,0,0)
        wait(lambda: not own_window(pid,title),"overlay failed to close")
        selected=controls(main,fields)
        require((selected["DraftPointX"],selected["DraftPointY"])==tuple(map(str,point)),"pressed point did not fill the draft")
        require(received()==before,"confirm click reached underlying window")
        events.append(dict(case="confirm",readout=text,pressed=point,released=after,draft=selected,target_messages=received()))
        for label in ("escape","closed"):
            _, overlay=start()
            require(own(overlay,point),"wrong overlay foreground")
            if label=="escape":
                user32.SetCursorPos(*after)
                send([vk_event(27),vk_event(27,True)])
            else:
                user32.PostMessageW(overlay,0x0010,0,0)
            wait(lambda: not own_window(pid,title),label+" did not clean up")
            current=controls(main,fields)
            require(all(current[k]==selected[k] for k in ("DraftPointX","DraftPointY","ScriptSource")),"cancellation changed draft")
            require(label in current["ScriptFeedback"],"missing matching cancellation reason")
            require(received()==before,"underlying window received an input during cancellation")
            events.append(dict(case=label,draft=current,target_messages=received()))
        bind_receiver(main,child.pid,folder)
        _, overlay=start()
        require(own(overlay,point),"binding pick missing its overlay")
        samples=report["bound_readout"]=[]
        def bound_ready():
            row=controls(overlay,["PointReadout"])
            samples.append(dict(overlay=int(overlay),front=user32.GetForegroundWindow(),control=row))
            if "PointReadout" not in row: return False
            user32.SetCursorPos(*after)
            time.sleep(.07)
            user32.SetCursorPos(*point)
            return readout()
        wait(bound_ready,"bound overlay coordinates ready before press")
        user32.mouse_event(2,0,0,0,0)
        time.sleep(.06)
        user32.mouse_event(4,0,0,0,0)
        wait(lambda: not own_window(pid,title),"bound point not closed")
        bound=controls(main,fields)
        require("客户区 (120, 200)" in bound["ScriptFeedback"] and "几何修订" in bound["ScriptFeedback"],"wrong bound point")
        require(received()==before,"bound target received confirmation")
        events.append(dict(case="bound",draft=bound,target_messages=received()))
        invoke(main,"InsertPoint")
        inserted=controls(main,fields)
        require("click_at(300, 400)" in inserted["ScriptSource"],"draft point was not usable")
        events.append(dict(case="insert",draft=inserted))
    finally:
        (folder/"point-observations.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf8")
        user32.SetCursorPos(cursor.x,cursor.y)
        local.send("quit")
        child.join(5)
        if child.is_alive(): child.terminate(); child.join(3)
    return 0


if __name__=="__main__":
    sys.exit(run(int(sys.argv[1]),int(sys.argv[2]),Path(sys.argv[3])))

