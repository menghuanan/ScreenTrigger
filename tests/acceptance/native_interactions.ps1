# Executed by verify-ui.ps1 inside one dedicated release application and data root.
Add-Type -AssemblyName System.Windows.Forms,WindowsBase
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class InteractionFixture {
  [StructLayout(LayoutKind.Sequential)] public struct Point {public int x,y;}
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out Point p);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int command);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(Point p);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h,uint flag);
  [DllImport("user32.dll")] public static extern uint GetDoubleClickTime();
  [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int key);
  [DllImport("user32.dll")] public static extern void mouse_event(uint mode, uint x, uint y, uint data, UIntPtr extra);
}
'@
function ByName([string]$name) { Wait-For { $e=Find-Item $name -Name; if ($e -and $e.Current.IsEnabled) {$e} } $name }
function NamedClick([string]$name) { (ByName $name).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }
function Modal-Key([string]$opener,[string]$title,[string]$case,[string]$choice='取消') {
    $control=ByName $opener
    $control.SetFocus()
    $null=Wait-For {$control.Current.HasKeyboardFocus} $opener
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    $null=ByName $title
    Save-Tree $case
    $button=ByName $choice
    $button.SetFocus()
    $null=Wait-For {$button.Current.HasKeyboardFocus -and $ae::FocusedElement.Current.ProcessId -eq $app.Id} $choice
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
    $seen[$case]=Wait-For { if ($control.Current.HasKeyboardFocus) {$control.Current.Name} } "$opener focus after $title"
}
Navigate NavScripts
Select-Item (ByName 'P2 调试')
NamedClick '打开脚本'
$null=Wait-For { (Item ScriptSource).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value -match 'local n=2' } 'script loaded'
Set-Field ScriptSource 'wait(8)'
Modal-Key '新建脚本' '保存脚本的修改？' 'script-draft'
Modal-Key '删除脚本' '删除脚本？' 'script-delete'
Invoke-ItemButton SaveScript
$null=Wait-For { (Find-Item ScriptFeedback).Current.Name -match '修订 2' } 'timed fixture saved'
Navigate NavTargets
$row=Wait-For { Find-Item '#1　P2 随机模板' -Name } 'target row'
Select-Item $row
NamedClick '打开目标'
$null=Wait-For { (Item TargetName).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value -eq 'P2 随机模板' } 'target loaded'
Set-Field TargetName 'P3 临时编辑'
$pointer=Join-Path $env:SIDECAR_DATA_DIR 'catalog/current.json'
$digestBefore=(Get-FileHash -Algorithm SHA256 -LiteralPath $pointer).Hash
$locked=[IO.File]::Open($pointer,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try {
    Invoke-ItemButton SaveTarget
    $seen.failedSave=Wait-For {
        $feedback=Find-Item Feedback
        if ($feedback.Current.Name -and $feedback.Current.Name -notmatch '已保存目标' -and (Find-Item SaveTarget).Current.IsEnabled) {$feedback.Current.Name}
    } 'save rejected while private current pointer denies replacement'
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $pointer).Hash -ne $digestBefore) {throw 'A failed save replaced current'}
    $seen.retainedDraft=(Item TargetName).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value
    if ($seen.retainedDraft -ne 'P3 临时编辑') {throw 'Failed save discarded the target draft'}
} finally {$locked.Dispose()}
Modal-Key '新建目标' '保存目标的修改？' 'target-draft'
Modal-Key '删除目标' '删除目标？' 'target-delete'
$expand=ByName '分组管理'
$expand.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Set-Field GroupName 'P3 待删除分组'
NamedClick '新建分组'
function Select-Option($box,[string]$text) {
    $expand=$box.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
    $row=Wait-For {
        if ($expand.Current.ExpandCollapseState -eq [Windows.Automation.ExpandCollapseState]::Collapsed) {$expand.Expand()}
        $box.FindFirst($tree,[Windows.Automation.PropertyCondition]::new($ae::NameProperty,$text))
    } $text
    Select-Item $row
}
Select-Option (Item TargetGroup) 'P3 待删除分组'
Set-Field TargetName 'P2 随机模板'
Invoke-ItemButton SaveTarget
$null=Wait-For { (Find-Item Feedback).Current.Name -match '已保存目标 #1' } 'target membership saved'
Select-Option (Item Groups) 'P3 待删除分组'
Modal-Key '删除分组' '删除分组？' 'group-delete' '删除'
$catalog=Join-Path $env:SIDECAR_DATA_DIR 'catalog'
function Read-TargetState {
    $ptr=Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $catalog 'current.json')|ConvertFrom-Json
    $gen=Join-Path $catalog ('generations/'+$ptr.generation)
    $t=(Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $gen 'targets.json')|ConvertFrom-Json).targets
    $g=(Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $gen 'groups.json')|ConvertFrom-Json).groups
    if (@($t).Count -eq 1 -and $null -eq $t[0].group_id -and @($g).Count -eq 0) {
        @{generation=$ptr.generation;target_id=$t[0].id;sample_no=$t[0].sample_no;group_id=$t[0].group_id}
    }
}
$seen.deletedGroup=Wait-For { Read-TargetState } 'group deletion retained its only target in ungrouped'
Navigate NavRun
[InteractionFixture]::ShowWindow($handle,3)|Out-Null
[InteractionFixture]::SetForegroundWindow($handle)|Out-Null
$button=Item ExecuteScript
$null=Wait-For {
    $button=Item ExecuteScript
    if ($button.Current.IsOffscreen) {
        $parent=$button
        while ($parent) {
            $scroll=$null
            if ($parent.TryGetCurrentPattern([Windows.Automation.ScrollPattern]::Pattern,[ref]$scroll) -and $scroll.Current.VerticallyScrollable) {
                $scroll.SetScrollPercent([Windows.Automation.ScrollPattern]::NoScroll,100); break
            }
            $parent=[Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($parent)
        }
    }
    if (!$button.Current.IsOffscreen) { $button.SetFocus(); $button.Current.HasKeyboardFocus }
} 'button in the dedicated foreground'
$history=Join-Path $env:SIDECAR_DATA_DIR 'history'
$before=@((Get-ChildItem -LiteralPath $history -Filter '*.json' -File).Name)
$old=[InteractionFixture+Point]::new()
if (![InteractionFixture]::GetCursorPos([ref]$old) -or [InteractionFixture]::GetAsyncKeyState(1) -lt 0) { throw 'Cannot safely perform double click' }
try {
    $scroll=$null
    $ancestor=$button
    while ($ancestor) {
        $pattern=$null
        if ($ancestor.TryGetCurrentPattern([Windows.Automation.ScrollPattern]::Pattern,[ref]$pattern) -and $pattern.Current.VerticallyScrollable) { $scroll=$pattern; break }
        $ancestor=[Windows.Automation.TreeWalker]::RawViewWalker.GetParent($ancestor)
    }
    $click=$null
    $probeRows=@()
    foreach ($offset in @(-1,0,25,50,75,100)) {
        if ($offset -ge 0 -and $scroll) {$scroll.SetScrollPercent([Windows.Automation.ScrollPattern]::NoScroll,$offset);Start-Sleep -Milliseconds 160}
        $rect=$button.Current.BoundingRectangle
        if ($rect.IsEmpty -or $rect.Width -le 0 -or $rect.Height -le 0) {continue}
        foreach ($factor in @(.5,.2,.8)) {
            $point=[Windows.Point]::new([Math]::Floor($rect.X+$rect.Width/2),[Math]::Floor($rect.Y+$rect.Height*$factor))
            $item=$ae::FromPoint($point)
            $names=@()
            while ($item -and $item.Current.ProcessId -eq $app.Id) {
                $names+=@($item.Current.AutomationId+':'+$item.Current.Name)
                if ($item.Current.AutomationId -eq 'ExecuteScript') {$click=$point;break}
                $item=[Windows.Automation.TreeWalker]::RawViewWalker.GetParent($item)
            }
            $probeRows+=@(@{rect=$rect.ToString();offset=$offset;path=$names})
            if ($click) {break}
        }
        if ($click) {break}
    }
    $seen.buttonProbe=$probeRows
    if (!$click) {
        # Some XAML providers expose only the owning desktop window in FromPoint.
        # Use the visible button rectangle, then verify the real native HWND and new execution files.
        $rect=$button.Current.BoundingRectangle
        $center=[Windows.Point]::new([Math]::Floor($rect.X+$rect.Width/2),[Math]::Floor($rect.Y+$rect.Height/2))
        if ($button.Current.IsOffscreen -or !$main.Current.BoundingRectangle.Contains($center)) {throw 'Run button outside the test window'}
        $click=$center
    }
    $p=[InteractionFixture+Point]::new()
    $p.x=[int]$click.X;$p.y=[int]$click.Y
    if (![InteractionFixture]::SetCursorPos($p.x,$p.y)) {throw 'Cannot target owned button'}
    $focused=$ae::FromPoint([Windows.Point]::new($p.x,$p.y))
    $hitPath=@()
    while ($focused -and $focused.Current.ProcessId -eq $app.Id) {
        $hitPath+=@($focused.Current.AutomationId+':'+$focused.Current.Name)
        if ($focused.Current.AutomationId -eq 'ExecuteScript') {break}
        $focused=[Windows.Automation.TreeWalker]::RawViewWalker.GetParent($focused)
    }
    $seen.buttonHit=@{x=$p.x;y=$p.y;ownControls=$hitPath;rect=$button.Current.BoundingRectangle.ToString()}
    if (!$hitPath.Count) {throw 'Physical point belongs to a different process'}
    $timing=[Diagnostics.Stopwatch]::StartNew()
    foreach ($n in 1..2) {
        if ([InteractionFixture]::GetForegroundWindow() -ne $handle -or [InteractionFixture]::GetAncestor([InteractionFixture]::WindowFromPoint($p),2) -ne $handle) {throw 'Dedicated main window lost ownership of button'}
        if (!$button.Current.BoundingRectangle.Contains($click)) {throw 'Run button moved between clicks'}
        if ($n -eq 2) {$seen.doubleClickIntervalMs=$timing.Elapsed.TotalMilliseconds}
        [InteractionFixture]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
        try {Start-Sleep -Milliseconds 20} finally {[InteractionFixture]::mouse_event(4,0,0,0,[UIntPtr]::Zero)}
        if ($n -eq 1) {Start-Sleep -Milliseconds ([Math]::Min(75,[int]([InteractionFixture]::GetDoubleClickTime()/4)))}
    }
} finally {[InteractionFixture]::SetCursorPos($old.x,$old.y)|Out-Null}
$null=Wait-For { (Find-Item RunStatus).Current.Name -match '已完成' } 'double click terminal'
$after=@(Get-ChildItem -LiteralPath $history -Filter '*.json' -File | Where-Object {$before -notcontains $_.Name})
$seen.doubleClick=@{intervalMs=[InteractionFixture]::GetDoubleClickTime();newRootFiles=@($after.Name);count=$after.Count}
if ($after.Count -ne 1) {throw "One physical double click created $($after.Count) new roots"}
$initial=@((Get-ChildItem -LiteralPath $history -Filter '*.json' -File).Name)
$ids=@()
foreach ($index in 1..3) {
    Invoke-ItemButton ExecuteScript
    $id=Wait-For {
        $message=(Find-Item Feedback).Current.Name
        if ($message -match '已受理执行 ([a-f0-9-]{36})' -and $ids -notcontains $Matches[1] -and
            $initial -notcontains ($Matches[1]+'.json')) {$Matches[1]}
    } "independent root $index"
    $ids+=$id
}
Invoke-ItemButton ExecuteScript
$seen.fourthRun=Wait-For { $message=(Find-Item Feedback).Current.Name; if ($message -match 'BUSY|并发|已满|上限|最多') {$message} } 'fourth root rejected'
$seen.threeIndependent=$ids
$null=Wait-For {
    $files=@(Get-ChildItem -LiteralPath $history -Filter '*.json' -File | Where-Object {$initial -notcontains $_.Name})
    $entries=@($files | ForEach-Object {Get-Content -Encoding UTF8 -Raw -LiteralPath $_.FullName | ConvertFrom-Json})
    $completed=@($entries|Where-Object {$_.status -eq 'completed' -and $ids -contains $_.execution_id})
    if ($completed.Count -eq 3 -and $files.Count -eq 3) {
        $latestStart=($completed|Sort-Object started_at|Select-Object -Last 1).started_at
        $earliestEnd=($completed|Sort-Object updated_at|Select-Object -First 1).updated_at
        if ([DateTimeOffset]$latestStart -ge [DateTimeOffset]$earliestEnd) {throw 'Independent runs did not overlap'}
        $completed|ConvertTo-Json -Depth 5|Set-Content -Encoding utf8 -LiteralPath (Join-Path $folder.FullName 'three-roots.json')
        $true
    }
} 'all three independent runs finished'
