param([Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$OutputDirectory,
      [ValidateSet('ready','error')][string]$Expect='ready',[switch]$ExplorerLaunch)
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
    [DllImport("user32")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32")] public static extern IntPtr GetAncestor(IntPtr h,uint flags);
    [DllImport("user32")] public static extern IntPtr WindowFromPoint(System.Drawing.Point point);
    [DllImport("user32")] public static extern bool SetCursorPos(int x,int y);
    [DllImport("user32")] public static extern bool GetCursorPos(out System.Drawing.Point point);
    [DllImport("user32")] public static extern short GetAsyncKeyState(int vk);
    [DllImport("user32")] public static extern void mouse_event(uint f,uint x,uint y,uint d,UIntPtr e);
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
        $hw=[IntPtr]$explorerView.HWND
        $item=$explorerView.Document.Folder.ParseName([IO.Path]::GetFileName($exe))
        if (!$item -or $item.Path -ne $exe) {throw 'The executable is absent in Explorer'}
        $explorerView.Document.SelectItem($item,29)
        [void][PortableShellInput]::SetForegroundWindow($hw)
        Start-Sleep -Milliseconds 200
        $root=$ae::FromHandle($hw)
        $selected=@($explorerView.Document.SelectedItems())
        if ($selected.Count -ne 1 -or $selected[0].Path -ne $exe) {throw 'Unexpected Shell selection'}
        $cond=[Windows.Automation.PropertyCondition]::new($ae::ControlTypeProperty,[Windows.Automation.ControlType]::ListItem)
        $matches=@($root.FindAll($scope,$cond) | Where-Object {
            $_.Current.Name -in @('ScreenTrigger','ScreenTrigger.exe') -and !$_.Current.IsOffscreen
        })
        if ($matches.Count -ne 1) {throw 'Explorer executable row is ambiguous'}
        $point=$matches[0].GetClickablePoint()
        $pos=New-Object Drawing.Point ([int]$point.X),([int]$point.Y)
        $priorPoint=New-Object Drawing.Point
        [void][PortableShellInput]::GetCursorPos([ref]$priorPoint)
        try {
            if ([PortableShellInput]::GetAsyncKeyState(1) -lt 0) {throw 'Left button already held'}
            if (![PortableShellInput]::SetCursorPos($pos.X,$pos.Y)) {throw 'Could not position the pointer'}
            for ($click=0;$click -lt 2;$click++) {
                if ([PortableShellInput]::GetForegroundWindow() -ne $hw -or [PortableShellInput]::GetAncestor([PortableShellInput]::WindowFromPoint($pos),2) -ne $hw) {
                    throw 'Foreground or hit test changed before Explorer click'
                }
                [PortableShellInput]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
                [PortableShellInput]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
                if ($click -eq 0) {Start-Sleep -Milliseconds 45}
            }
        } finally {
            [void][PortableShellInput]::SetCursorPos($priorPoint.X,$priorPoint.Y)
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
    do {
        $app.Refresh()
        if ($app.HasExited) { throw "Application exited before $Expect" }
        $hwnd=$app.MainWindowHandle
        if ($hwnd -ne [IntPtr]::Zero) {
            $main=$ae::FromHandle($hwnd)
            $stop=$main.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'StopEngine'))
            $tree=@($main.FindAll($scope,[Windows.Automation.Condition]::TrueCondition) | ForEach-Object {
                [ordered]@{id=$_.Current.AutomationId;name=$_.Current.Name}
            })
            $record.tree=$tree
            if ($Expect -eq 'ready' -and $stop -and $stop.Current.IsEnabled) {break}
            if ($Expect -eq 'error' -and (@($tree | ForEach-Object { $_.name }) -match '不可写|哈希不符|长度不符|便携清单|便携数据布局|复制到可写|便携目录')) {break}
        }
        if ($sw.Elapsed.TotalSeconds -gt 120) { throw "Timeout waiting for $Expect" }
        Start-Sleep -Milliseconds 100
    } while ($true)
    $record.elapsed_ms=$sw.ElapsedMilliseconds
    $record.status=$Expect
    $main.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (!$app.WaitForExit(15000)) {throw 'Application did not close within 15 seconds'}
} finally {
    if ($app -and !$app.HasExited) { [void]$app.CloseMainWindow(); if (!$app.WaitForExit(5000)) {$app.Kill()} }
    if ($exclusiveView -and $explorerView.Document.Folder.Self.Path -eq $dir) {$explorerView.Quit()}
    $record | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 (Join-Path $folder.FullName 'window.json')
}
