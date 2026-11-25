# Operate only the dedicated release process started by verify-ui.ps1.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class KeyboardFixture {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr handle, int command);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
}
'@
[void][KeyboardFixture]::ShowWindow($handle,3)
[void][KeyboardFixture]::SetForegroundWindow($handle)
function Send-OwnedKey([string]$keys,[IntPtr]$window=$handle) {
    if ([KeyboardFixture]::GetForegroundWindow() -ne $window -or $ae::FocusedElement.Current.ProcessId -ne $app.Id) {
        throw "Keyboard focus left the test window before $keys"
    }
    [System.Windows.Forms.SendKeys]::SendWait($keys)
}
function Press-Control($element,[string]$keys='{ENTER}') {
    $element.SetFocus()
    $null=Wait-For { $element.Current.HasKeyboardFocus } $element.Current.Name
    Send-OwnedKey $keys
}
function Select-ByKey([string]$nav,[string]$marker) {
    $order=@($pages.Keys)
    function Focused-Nav {
        $p=$ae::FocusedElement
        while ($p -and $p.Current.ProcessId -eq $app.Id) {
            if ($order -contains $p.Current.AutomationId) {return $p.Current.AutomationId}
            $p=[Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($p)
        }
    }
    for ($i=0;$i -lt 256 -and !(Focused-Nav);$i++) {
        Send-OwnedKey '{TAB}'
        Start-Sleep -Milliseconds 40
    }
    $at=Focused-Nav
    if (!$at) {throw "Did not reach the top navigation before $nav"}
    for ($i=0;$i -lt $order.Count -and $at -ne $nav;$i++) {
        $direction=if ($order.IndexOf($at) -lt $order.IndexOf($nav)) {'{RIGHT}'} else {'{LEFT}'}
        Send-OwnedKey $direction
        $at=Wait-For { $new=Focused-Nav; if ($new -and $new -ne $at) {$new} } "$direction from $at"
    }
    if ($at -ne $nav) {throw "Keyboard navigation reached $at instead of $nav"}
    Send-OwnedKey '{ENTER}'
    $null=Wait-For { $m=Find-Item $marker; if ($m) {$m} } $marker
}
function Trace-TabOrder([string]$nav) {
    $path=[Collections.Generic.List[object]]::new()
    $visited=[Collections.Generic.HashSet[string]]::new()
    $repeated=$false
    for ($turn=0;$turn -lt 256;$turn++) {
        Send-OwnedKey '{TAB}'
        Start-Sleep -Milliseconds 45
        $e=$ae::FocusedElement
        if ($e.Current.ProcessId -ne $app.Id) {throw "Tab left the dedicated process on $nav"}
        $key=$e.GetRuntimeId() -join '.'
        if (!$visited.Add($key)) {$repeated=$true;break}
        $p=$e
        $ancestry=@()
        while ($p -and $p.Current.ProcessId -eq $app.Id) {
            $ancestry+=@($p.Current.AutomationId)
            $p=[Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($p)
        }
        $path.Add([ordered]@{id=$e.Current.AutomationId;name=$e.Current.Name;
                            type=$e.Current.ControlType.ProgrammaticName; ancestors=@($ancestry|Where-Object {$_})})
    }
    $path|ConvertTo-Json -Depth 5|Set-Content -Encoding utf8 -LiteralPath (Join-Path $folder.FullName "$nav-tabs.json")
    if (!$repeated -or $path.Count -lt 2) {throw "Incomplete native Tab cycle on $nav"}
    $cycles[$nav]=$path.Count
}
$cycles=[ordered]@{}
$pages=[ordered]@{NavEngine='Diagnostics';NavImages='StartSelection';NavTargets='TargetName';NavScripts='ScriptSource';
    NavRecognition='TestRecognition';NavRun='ExecuteScript';NavMonitor='StartMonitor';NavHistory='RefreshHistory';NavSettings='SaveSettings'}
foreach ($page in $pages.GetEnumerator()) {
    Select-ByKey $page.Key $page.Value
    Save-Tree ($page.Key+'-keyboard')
    Trace-TabOrder $page.Key
}
$seen.tabStops=$cycles
function Cancel-DialogByKey([string]$id,[string]$title,[string]$case) {
    $opener=Item $id
    Press-Control $opener
    $null=Wait-For {Find-Item $title -Name} $title
    Save-Tree $case
    Send-OwnedKey '{ESC}'
    $seen[$case]=Wait-For {if ($opener.Current.HasKeyboardFocus) {$opener.Current.Name}} "$id regained focus"
}
Select-ByKey NavSettings SaveSettings
$captureSettings=Item SettingGroup_capture
$captureSettings.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Set-Field Setting_capture_preview_hz '12'
Invoke-ItemButton SaveSettings
$null=Wait-For {(Find-Item SettingsFeedback).Current.Name -match '设置已保存'} 'non-default value for reset preview'
Cancel-DialogByKey ResetSettings '恢复默认设置？' 'keyboard-reset'
Select-ByKey NavHistory RefreshHistory
Invoke-ItemButton RefreshHistory
$null=Wait-For { (Find-Item HistoryLogs).Current.Name -match 'failed|completed' } 'fixture history'
Cancel-DialogByKey Clear '清除这次执行的日志？' 'keyboard-clear'
Select-ByKey NavEngine Diagnostics
$diagnostics=Item Diagnostics
Press-Control $diagnostics
$diag=Wait-For {
    $condition=[Windows.Automation.PropertyCondition]::new($ae::ProcessIdProperty,$app.Id)
    $ae::RootElement.FindAll([Windows.Automation.TreeScope]::Children,$condition) | Where-Object {$_.Current.Name -eq 'ScreenTrigger · 引擎诊断'} | Select-Object -First 1
} 'owned diagnostics window'
@($diag.FindAll($tree,[Windows.Automation.Condition]::TrueCondition)|ForEach-Object {
    @{id=$_.Current.AutomationId;name=$_.Current.Name;type=$_.Current.ControlType.ProgrammaticName;keyboard=$_.Current.IsKeyboardFocusable}
})|ConvertTo-Json|Set-Content -Encoding utf8 -LiteralPath (Join-Path $folder.FullName 'keyboard-diagnostics.json')
$diagHandle=[IntPtr]$diag.Current.NativeWindowHandle
$null=Wait-For {[KeyboardFixture]::GetForegroundWindow() -eq $diagHandle} 'diagnostics foreground'
Send-OwnedKey '%{F4}' $diagHandle
$seen.diagnosticsFocus=Wait-For {if ($diagnostics.Current.HasKeyboardFocus) {$diagnostics.Current.Name}} 'diagnostics returned keyboard focus'

Select-ByKey NavScripts ScriptSource
function New-DebugScript([string]$name,[string]$source) {
    Press-Control (Wait-For {Find-Item '新建脚本' -Name} 'new script button')
    Set-Field ScriptName $name
    Set-Field ScriptSource $source
    Invoke-ItemButton SaveScript
    $null=Wait-For {(Find-Item ScriptFeedback).Current.Name -match '修订 1'} 'new script saved'
    $cat=Join-Path $env:SIDECAR_DATA_DIR 'catalog'
    $ptr=Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $cat 'current.json')|ConvertFrom-Json
    $scripts=Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $cat ('generations/'+$ptr.generation+'/script_metadata.json'))|ConvertFrom-Json
    $s=@($scripts.scripts|Where-Object name -eq $name)
    if ($s.Count -ne 1) {throw "Cannot identify the saved $name"}
    $s[0].id
}
$child=New-DebugScript 'P3 键盘子脚本' ("local x=5"+[char]10+"x=x+1"+[char]10+"return x")
$parent=New-DebugScript 'P3 键盘主脚本' ("local n=2"+[char]10+'local answer=call_script("'+$child+'")'+[char]10+"return answer+n")
Press-Control (Item OpenDebug)
Set-Field BreakpointLine '2'
Invoke-ItemButton AddBreakpoint
$null=Wait-For {Find-Item '第 2 行' -Name} 'parent line 2 breakpoint'
foreach ($mode in @('StepInto','StepOver')) {
    Press-Control (Item StartDebug)
    $null=Wait-For {(Find-Item DebugVariables).Current.Name -match 'n.*2' -and (Find-Item PausedSource).Current.Name -match '键盘主脚本'} 'pause in parent before call'
    Press-Control (Item $mode)
    if ($mode -eq 'StepInto') {
        $null=Wait-For {(Find-Item PausedSource).Current.Name -match '键盘子脚本'} 'pause in child'
        $frames=Item Frames
        $expand=$frames.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
        $expand.Expand()
        $childFrame=Wait-For {$frames.FindAll($tree,[Windows.Automation.Condition]::TrueCondition)|Where-Object {$_.Current.Name -match [regex]::Escape($child)}|Select-Object -First 1} 'child frame'
        $parentFrame=Wait-For {$frames.FindAll($tree,[Windows.Automation.Condition]::TrueCondition)|Where-Object {$_.Current.Name -match [regex]::Escape($parent)}|Select-Object -First 1} 'parent frame'
        $seen.debugFrames=@($childFrame.Current.Name,$parentFrame.Current.Name)
        Select-Item $parentFrame
        $seen.parentVariables=Wait-For {$v=(Find-Item DebugVariables).Current.Name;if ($v -match 'n.*2') {$v}} 'caller local at child breakpoint'
        Save-Tree 'keyboard-child-stack'
        Press-Control (Item StepOut)
    }
    $seen[$mode]=Wait-For {$v=(Find-Item PausedSource).Current.Name;if ($v -match '键盘主脚本' -and $v -match '▶\s+3\s+return answer\+n') {$v}} 'back at parent return'
    Press-Control (Item ContinueDebug)
    $null=Wait-For {(Find-Item DebugStatus).Current.Name -match '已完成'} 'debug run completed'
    $null=Wait-For {$ae::FocusedElement.Current.AutomationId -eq 'ScriptSource'} 'editor focused after debug'
}
