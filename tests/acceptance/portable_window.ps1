param([Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$OutputDirectory,
      [ValidateSet('ready','error')][string]$Expect='ready',[switch]$ExplorerLaunch,[string]$StartupScenario)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$folder=New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop
$exe=(Resolve-Path -LiteralPath $Executable).Path
$scope=[Windows.Automation.TreeScope]::Descendants
$ae=[Windows.Automation.AutomationElement]
$record=[ordered]@{executable=$exe;expected=$Expect}
$app=$null
$explorerView=$null
$exclusiveView=$false
try {
    if ($ExplorerLaunch) {
        Add-Type -AssemblyName WindowsBase
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PortableShellInput {
    [DllImport("user32")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32")] public static extern bool ShowWindow(IntPtr window,int command);
    [DllImport("user32")] public static extern bool SetWindowPos(IntPtr window,IntPtr after,int x,int y,int cx,int cy,uint flags);
    [DllImport("user32")] public static extern int GetSystemMetrics(int index);
    [DllImport("kernel32")] public static extern uint GetCurrentThreadId();
    [DllImport("user32")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd,IntPtr process);
    [DllImport("user32")] public static extern bool AttachThreadInput(uint source,uint target,bool attach);
    [DllImport("user32")] public static extern bool BringWindowToTop(IntPtr window);
    [DllImport("user32")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32")] public static extern IntPtr GetAncestor(IntPtr h,uint flags);
    [DllImport("user32")] public static extern IntPtr WindowFromPoint(System.Drawing.Point point);
    [DllImport("user32")] public static extern bool SetCursorPos(int x,int y);
    [DllImport("user32")] public static extern bool GetCursorPos(out System.Drawing.Point point);
    [DllImport("user32")] public static extern short GetAsyncKeyState(int vk);
    [DllImport("user32")] public static extern void mouse_event(uint f,uint x,uint y,uint d,UIntPtr e);
    public static long SecondDown() {
        long tick = System.Diagnostics.Stopwatch.GetTimestamp();
        mouse_event(2,0,0,0,UIntPtr.Zero);
        return tick;
    }
}
'@ -ReferencedAssemblies System.Drawing
        $shell=New-Object -ComObject Shell.Application
        $prior=@($shell.Windows() | ForEach-Object { $_.HWND })
        $dir=[IO.Path]::GetDirectoryName($exe)
        $preexisting=@(Get-Process ScreenTrigger -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        $shell.Explore($dir)
        $timeout=[Diagnostics.Stopwatch]::StartNew()
        do {
            foreach ($view in $shell.Windows()) {
                try {
                    if ($view.Document.Folder.Self.Path -eq $dir) {$explorerView=$view;break}
                } catch {}
            }
            if ($timeout.Elapsed.TotalSeconds -gt 15) {throw 'Could not find the exact Explorer folder'}
            Start-Sleep -Milliseconds 100
        } until ($null -ne $explorerView)
        $exclusiveView=$prior -notcontains $explorerView.HWND
        if ($StartupScenario -and !$exclusiveView) { throw 'The timing scenario requires a newly opened dedicated Explorer window' }
        $hw=[IntPtr]$explorerView.HWND
        if ($StartupScenario) {
            [void][PortableShellInput]::SetThreadDpiAwarenessContext([IntPtr](-4))
            if ($exclusiveView) {
                [void][PortableShellInput]::ShowWindow($hw,9)
                $width=[PortableShellInput]::GetSystemMetrics(0)
                $height=[PortableShellInput]::GetSystemMetrics(1)
                if (![PortableShellInput]::SetWindowPos($hw,[IntPtr](-1),[int]($width*.51),[int]($height*.08),[int]($width*.46),[int]($height*.8),16)) {throw 'Cannot display the dedicated Explorer window'}
                $explorerView.Document.CurrentViewMode=2
            }
        }
        $item=$explorerView.Document.Folder.ParseName([IO.Path]::GetFileName($exe))
        if (!$item -or $item.Path -ne $exe) {throw 'The executable is absent in Explorer'}
        $explorerView.Document.SelectItem($item,29)
        [void][PortableShellInput]::SetForegroundWindow($hw)
        Start-Sleep -Milliseconds 200
        $root=$ae::FromHandle($hw)
        if ($StartupScenario) {
            $fg=[PortableShellInput]::GetForegroundWindow()
            $record.explorer_hwnd=$hw.ToInt64()
            $record.foreground_before=$fg.ToInt64()
            $record.foreground_process=$ae::FromHandle($fg).Current.ProcessId
            if (($fg -ne $hw) -and $exclusiveView) {
                $mine=[PortableShellInput]::GetCurrentThreadId()
                $other=[PortableShellInput]::GetWindowThreadProcessId($fg,[IntPtr]::Zero)
                $joined=[PortableShellInput]::AttachThreadInput($mine,$other,$true)
                try {
                    [void][PortableShellInput]::BringWindowToTop($hw)
                    $record.focus_requested=[PortableShellInput]::SetForegroundWindow($hw)
                } finally {if ($joined) {[void][PortableShellInput]::AttachThreadInput($mine,$other,$false)} }
                $fg=[PortableShellInput]::GetForegroundWindow()
            }
            if (($fg -ne $hw) -and $exclusiveView) {
                $rect=$root.Current.BoundingRectangle
                foreach ($offset in @([int]($rect.Width*.55),320,220,100)) {
                    $header=New-Object Drawing.Point ([int]($rect.Left+$offset)),([int]($rect.Top+20))
                    if ([PortableShellInput]::GetAncestor([PortableShellInput]::WindowFromPoint($header),2) -ne $hw) {continue}
                    $restore=New-Object Drawing.Point
                    [void][PortableShellInput]::GetCursorPos([ref]$restore)
                    try {
                        if ([PortableShellInput]::GetAsyncKeyState(1) -lt 0) {throw 'Left button already held'}
                        if (![PortableShellInput]::SetCursorPos($header.X,$header.Y)) {throw 'Cannot focus owned Explorer'}
                        if ([PortableShellInput]::GetAncestor([PortableShellInput]::WindowFromPoint($header),2) -ne $hw) {throw 'Explorer focus location changed'}
                        [PortableShellInput]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
                        [PortableShellInput]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
                    } finally { [void][PortableShellInput]::SetCursorPos($restore.X,$restore.Y) }
                    Start-Sleep -Milliseconds 75
                    if ([PortableShellInput]::GetForegroundWindow() -eq $hw) {break}
                }
            }
        }
        $selected=@($explorerView.Document.SelectedItems())
        if ($StartupScenario) {
            $viewWait=[Diagnostics.Stopwatch]::StartNew()
            while (($selected.Count -ne 1 -or $selected[0].Path -ne $exe) -and $viewWait.ElapsedMilliseconds -lt 3000) {
                $explorerView.Document.SelectItem($item,29)
                Start-Sleep -Milliseconds 75
                $selected=@($explorerView.Document.SelectedItems())
            }
        }
        if ($selected.Count -ne 1 -or $selected[0].Path -ne $exe) {throw 'Unexpected Shell selection'}
        $cond=[Windows.Automation.PropertyCondition]::new($ae::ControlTypeProperty,[Windows.Automation.ControlType]::ListItem)
        $matches=@($root.FindAll($scope,$cond) | Where-Object {
            $_.Current.Name -in @('ScreenTrigger','ScreenTrigger.exe') -and !$_.Current.IsOffscreen
        })
        if ($matches.Count -ne 1) {throw 'Explorer executable row is ambiguous'}
        if ($StartupScenario) {
            $row=$matches[0]
            $row.GetCurrentPattern([Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
            Start-Sleep -Milliseconds 125
        }
        try { $point=$matches[0].GetClickablePoint() } catch {
            if (!$StartupScenario) {throw}
            $rect=$matches[0].Current.BoundingRectangle
            $record.explorer_row=$rect.ToString()
            $record.explorer_bounds=$root.Current.BoundingRectangle.ToString()
            $point=$null
            foreach ($offset in @(12,24,40,64,96)) {
                $probe=[Windows.Point]::new($rect.Left+[Math]::Min($offset,$rect.Width/2),$rect.Top+$rect.Height/2)
                $element=$ae::FromPoint($probe)
                $record.last_hit=[ordered]@{id=$element.Current.AutomationId;pid=$element.Current.ProcessId;class=$element.Current.ClassName}
                while ($element -and ![Windows.Automation.Automation]::Compare($root,$element)) {
                    if ([Windows.Automation.Automation]::Compare($matches[0],$element)) { $point=$probe;break }
                    $element=[Windows.Automation.TreeWalker]::RawViewWalker.GetParent($element)
                }
                if ($point) {break}
            }
            if (!$point) {throw 'No verified visible point on the exact Explorer item'}
        }
        $pos=New-Object Drawing.Point ([int]$point.X),([int]$point.Y)
        $priorPoint=New-Object Drawing.Point
        [void][PortableShellInput]::GetCursorPos([ref]$priorPoint)
        try {
            if ([PortableShellInput]::GetAsyncKeyState(1) -lt 0) {throw 'Left button already held'}
            if (![PortableShellInput]::SetCursorPos($pos.X,$pos.Y)) {throw 'Could not position the pointer'}
            if ($StartupScenario) { $record.physical_clicks_sent=0; $record.preflight_focus_retries=0 }
            for ($click=0;$click -lt 2;$click++) {
                if ($StartupScenario -and $click -eq 0) {
                    while ([PortableShellInput]::GetForegroundWindow() -ne $hw -and $record.preflight_focus_retries -lt 3) {
                        $other=[PortableShellInput]::GetWindowThreadProcessId([PortableShellInput]::GetForegroundWindow(),[IntPtr]::Zero)
                        $mine=[PortableShellInput]::GetCurrentThreadId()
                        $joined=[PortableShellInput]::AttachThreadInput($mine,$other,$true)
                        try {
                            [void][PortableShellInput]::BringWindowToTop($hw)
                            [void][PortableShellInput]::SetForegroundWindow($hw)
                        } finally { if ($joined) {[void][PortableShellInput]::AttachThreadInput($mine,$other,$false)} }
                        $record.preflight_focus_retries++
                        Start-Sleep -Milliseconds 25
                    }
                }
                if ([PortableShellInput]::GetForegroundWindow() -ne $hw -or [PortableShellInput]::GetAncestor([PortableShellInput]::WindowFromPoint($pos),2) -ne $hw) {
                    throw 'Foreground or hit test changed before Explorer click'
                }
                if ($StartupScenario) {
                    $hit=$ae::FromPoint($point)
                    while ($hit -and ![Windows.Automation.Automation]::Compare($matches[0],$hit)) {
                        if ([Windows.Automation.Automation]::Compare($root,$hit)) { break }
                        $hit=[Windows.Automation.TreeWalker]::RawViewWalker.GetParent($hit)
                    }
                    if (!$hit -or ![Windows.Automation.Automation]::Compare($matches[0],$hit)) {throw 'Selected executable row no longer owns the physical point'}
                    $currentPoint=New-Object Drawing.Point
                    if (![PortableShellInput]::GetCursorPos([ref]$currentPoint) -or $currentPoint -ne $pos -or [PortableShellInput]::GetForegroundWindow() -ne $hw) {throw 'The physical cursor or foreground changed; no click dispatched'}
                }
                if ($StartupScenario -and $click -eq 1) {
                    $record.anchor_before_qpc=[Diagnostics.Stopwatch]::GetTimestamp()
                    $record.qpc_utc_anchor=[DateTime]::UtcNow.ToString('o')
                    $record.frequency=[Diagnostics.Stopwatch]::Frequency
                    $record.anchor_after_qpc=[Diagnostics.Stopwatch]::GetTimestamp()
                    $record.second_down_qpc=[PortableShellInput]::SecondDown()
                } else {
                    [PortableShellInput]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
                }
                [PortableShellInput]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
                if ($StartupScenario) { $record.physical_clicks_sent++ }
                if ($click -eq 0) {Start-Sleep -Milliseconds 45}
            }
        } finally {
            [void][PortableShellInput]::SetCursorPos($priorPoint.X,$priorPoint.Y)
            if ($StartupScenario -and $exclusiveView) { [void][PortableShellInput]::SetWindowPos($hw,[IntPtr](-2),0,0,0,0,19) }
        }
        $sw=[Diagnostics.Stopwatch]::StartNew()
        do {
            $found=@(Get-Process ScreenTrigger -ErrorAction SilentlyContinue | Where-Object {$_.Id -notin $preexisting -and $_.Path -eq $exe})
            if ($found.Count -gt 1 -or $sw.Elapsed.TotalSeconds -gt 30) {throw 'Unexpected application launch count'}
            if ($found.Count -eq 1) {$app=$found[0];break}
            Start-Sleep -Milliseconds 80
        } while ($true)
        $record.explorer=[ordered]@{hwnd=$hw.ToInt64();selected=$item.Path;x=$pos.X;y=$pos.Y;clicks=2}
    } else {
        $app=Start-Process -FilePath $exe -WorkingDirectory $env:SystemRoot -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $folder.FullName 'stderr.log')
        $sw=[Diagnostics.Stopwatch]::StartNew()
        $record.working_directory=$env:SystemRoot
    }
    $record.pid=$app.Id
    if ($StartupScenario) {
        if (-not $ExplorerLaunch) {throw 'Startup measurement requires Explorer'}
        . (Join-Path $PSScriptRoot 'startup_run_ui.ps1')
        Measure-StartupUi $app $record $StartupScenario
        return
    }
    do {
        $app.Refresh()
        if ($app.HasExited) { throw "Application exited before $Expect" }
        $hwnd=$app.MainWindowHandle
        if ($hwnd -ne [IntPtr]::Zero) {
            $main=$ae::FromHandle($hwnd)
            $stop=$main.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'StopEngine'))
            $newScript=$main.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'NewScript'))
            $tree=@($main.FindAll($scope,[Windows.Automation.Condition]::TrueCondition) | ForEach-Object {
                [ordered]@{id=$_.Current.AutomationId;name=$_.Current.Name}
            })
            $record.tree=$tree
            if ($Expect -eq 'ready' -and (($stop -and $stop.Current.IsEnabled) -or ($newScript -and $newScript.Current.IsEnabled))) {break}
            if ($Expect -eq 'error' -and (@($tree | ForEach-Object { $_.name }) -match '不可写|哈希不符|长度不符|便携清单|便携数据布局|复制到可写|便携目录')) {break}
        }
        if ($sw.Elapsed.TotalSeconds -gt 120) { throw "Timeout waiting for $Expect" }
        Start-Sleep -Milliseconds 100
    } while ($true)
    $record.elapsed_ms=$sw.ElapsedMilliseconds
    $record.status=$Expect
    $main.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (!$app.WaitForExit(15000)) {throw 'Application did not close within 15 seconds'}
} catch {
    $record.error=$_.Exception.Message
    throw
} finally {
    if ($app -and !$app.HasExited) { [void]$app.CloseMainWindow(); if (!$app.WaitForExit(5000)) {$app.Kill()} }
    if ($exclusiveView -and $explorerView.Document.Folder.Self.Path -eq $dir) {$explorerView.Quit()}
    $record | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 (Join-Path $folder.FullName 'window.json')
}
