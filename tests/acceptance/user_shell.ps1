# Run against a fresh, disposable extraction or an isolated Debug data root.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Exe, [Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Windows.Forms,System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class UserShellProbe {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out int id);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hwnd);
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X,Y; }
    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(Point position);
    public static IntPtr At(int x,int y) { return WindowFromPoint(new Point { X=x, Y=y }); }
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
}
'@
$ae = [Windows.Automation.AutomationElement]
$scope = [Windows.Automation.TreeScope]::Descendants
$exeFile = Get-Item -LiteralPath $Exe
$folder = New-Item -ItemType Directory -Path $Output
if (Test-Path (Join-Path $exeFile.DirectoryName 'ScreenTrigger.portable.json')) {
    if (Test-Path (Join-Path $exeFile.DirectoryName 'data')) { throw 'Use a fresh extraction.' }
} else {
    $env:SIDECAR_DATA_DIR = Join-Path $folder.FullName 'test-data'
}
$record = [ordered]@{exe=$exeFile.FullName; started=(Get-Date).ToString('o'); steps=@(); os=[Environment]::OSVersion.Version.ToString()}
function Observe($step) { $record.steps += $step; Write-Output $step | Out-Host }
function Find-Control([string]$value, [switch]$ByName) {
    $property = if ($ByName) { $ae::NameProperty } else { $ae::AutomationIdProperty }
    $condition = [Windows.Automation.PropertyCondition]::new($property, $value)
    $root.FindFirst($scope,$condition)
}
function Wait-For([scriptblock]$predicate, [string]$label) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        if ($app.HasExited) { throw "App exited ($($app.ExitCode)) waiting for $label" }
        try { if ($v = & $predicate) { return $v } } catch [Windows.Automation.ElementNotAvailableException] {}
        Start-Sleep -Milliseconds 70
    } while ($timer.Elapsed.TotalSeconds -lt 35)
    throw "Timeout waiting for $label"
}
function Control([string]$id) { Wait-For { Find-Control $id } $id }
function Click-Control($el) { $el.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }
function Click-Id($id) { Click-Control (Wait-For { $el=Find-Control $id; if ($el -and $el.Current.IsEnabled) { $el } } "$id enabled") }
function Click-Name($name) { Click-Control (Wait-For { Find-Control $name -ByName } $name) }
function Set-Value($id,$value) { (Control $id).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).SetValue($value) }
function Read-Value($id) { (Control $id).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value }
function Navigate-To($id) { (Control $id).GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select() }
function Row-Control($prefix) {
    Wait-For {
        $root.FindAll($scope,[Windows.Automation.Condition]::TrueCondition) | Where-Object { $_.Current.IsEnabled -and $_.Current.AutomationId.StartsWith($prefix) } | Select-Object -First 1
    } $prefix
}
function Send-Owned($el,$keys) {
    [void][UserShellProbe]::SetForegroundWindow($app.MainWindowHandle)
    $null = Wait-For {
        if ($el.Current.IsEnabled -and $el.Current.IsKeyboardFocusable) {
            try { $el.SetFocus(); return $true } catch [System.Management.Automation.MethodInvocationException] { return $false }
        }
    } 'enabled keyboard control'
    $null = Wait-For { $active = 0; [void][UserShellProbe]::GetWindowThreadProcessId([UserShellProbe]::GetForegroundWindow(),[ref]$active)
        $active -eq $app.Id -and $ae::FocusedElement.Current.ProcessId -eq $app.Id } 'owned foreground and focus'
    [Windows.Forms.SendKeys]::SendWait($keys)
}
function Screenshot($name) {
    $hwnd = $app.MainWindowHandle
    $size = $root.Current.BoundingRectangle
    $bmp = [Drawing.Bitmap]::new([int]$size.Width,[int]$size.Height)
    $graphics = [Drawing.Graphics]::FromImage($bmp)
    $dc = $graphics.GetHdc()
    try { if (-not [UserShellProbe]::PrintWindow($hwnd,$dc,2)) { throw 'PrintWindow failed' } }
    finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
    try { $bmp.Save((Join-Path $folder.FullName "$name.png"),[Drawing.Imaging.ImageFormat]::Png) }
    finally { $bmp.Dispose() }
}
function Start-Client {
    $script:app = Start-Process -FilePath $exeFile.FullName -WorkingDirectory $env:SystemRoot -PassThru -WindowStyle Hidden
    $script:root = Wait-For { $app.Refresh(); if ($app.MainWindowHandle -ne [IntPtr]::Zero) { $ae::FromHandle($app.MainWindowHandle) } } 'first window'
    $null = Wait-For { $b = Find-Control 'NewScript'; $b -and $b.Current.IsEnabled } 'ready script controls'
}
function Close-Client {
    $root.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (-not $app.WaitForExit(15000)) { throw 'Application did not close' }
    if ($app.ExitCode -ne 0) { throw "Application exited $($app.ExitCode)" }
}
try {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Start-Client
    $record.ready_ms = $clock.ElapsedMilliseconds
    $record.dpi = [UserShellProbe]::GetDpiForWindow($app.MainWindowHandle)
    $record.display = @([Windows.Forms.Screen]::AllScreens | ForEach-Object { "$($_.DeviceName) $($_.Bounds)" })
    foreach ($id in 'NavScripts','NavGroups','NavLogs','NavAbout') { $null = Control $id }
    if (Find-Control 'EditScripts') { throw 'Edit visible with empty library' }
    Screenshot 'scripts-empty'
    Observe 'Fresh library is empty. New is enabled and Edit is hidden.'
    Click-Id 'NewScript'
    Set-Value 'ScriptName' '窗口测试'
    Set-Value 'ScriptSource' "log('user-shell'); wait(8)"
    Click-Id 'BackScripts'
    Click-Name '继续编辑'
    $null = Wait-For { -not (Find-Control '继续编辑' -ByName) } 'closed draft dialog'
    $null = Wait-For { (Read-Value 'ScriptName') -eq '窗口测试' } 'preserved draft'
    Click-Id 'BackScripts'
    Click-Name '保存并退出'
    $run = Row-Control 'Run_'
    $rowId = $run.Current.AutomationId.Substring(4)
    $key = Control "Key_$rowId"
    Send-Owned $key '%{DOWN}{END}{ENTER}'
    $null = Wait-For { $key.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0].Current.Name -eq 'F12' } 'F12 bound'
    Send-Owned $key '%{DOWN}{UP}{ESC}'
    Screenshot 'scripts'
    Observe 'Created through save-on-exit. F12 selected, then dropdown dismissed without changing the selection.'
    Click-Control $run
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '停止' } 'running after click'
    Send-Owned (Control 'NewScript') '{F12}'
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '开始' } 'stopped by F12'
    Send-Owned (Control 'NewScript') '{F12}'
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '停止' } 'started by F12'
    Click-Id "Run_$rowId"
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '开始' } 'stopped by click'
    Observe 'Button and window hotkey both start and stop the running Lua script.'
    Send-Owned $key '%{DOWN}{HOME}{DOWN}{ENTER}'
    $null = Wait-For { $key.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0].Current.Name -eq 'A' } 'A binding'
    Send-Owned $key '%{DOWN}{DOWN}'
    $bounds = (Control 'NewScript').Current.BoundingRectangle
    $position = [Drawing.Point]::new([int]($bounds.Left + $bounds.Width / 2),[int]($bounds.Top + $bounds.Height / 2))
    $targetPid = 0
    [void][UserShellProbe]::GetWindowThreadProcessId([UserShellProbe]::At($position.X,$position.Y),[ref]$targetPid)
    if ($targetPid -ne $app.Id) { throw 'Outside-click destination does not belong to the test window' }
    $previousPosition = [Windows.Forms.Cursor]::Position
    try {
        [Windows.Forms.Cursor]::Position = $position
        [UserShellProbe]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
        [UserShellProbe]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
    } finally { [Windows.Forms.Cursor]::Position = $previousPosition }
    if (Find-Control 'BackScripts') { Click-Id 'BackScripts' }
    $null = Wait-For { (Control "Key_$rowId").GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0].Current.Name -eq 'A' } 'outside click retained A'
    $other = [Windows.Forms.Form]::new()
    $inputBox = [Windows.Forms.TextBox]::new()
    $inputBox.ImeMode = [Windows.Forms.ImeMode]::Disable
    try {
        $other.Text = 'ScreenTrigger 输入验证'
        $other.Controls.Add($inputBox)
        $other.Show(); $other.Activate(); [void]$inputBox.Focus()
        [Windows.Forms.Application]::DoEvents()
        if ([UserShellProbe]::GetForegroundWindow() -ne $other.Handle -or -not $inputBox.Focused) { throw 'Fixture does not own input focus' }
        [UserShellProbe]::keybd_event(65,0,0,[UIntPtr]::Zero)
        [UserShellProbe]::keybd_event(65,0,2,[UIntPtr]::Zero)
        $inputWait = [Diagnostics.Stopwatch]::StartNew()
        while ($inputBox.Text.Length -eq 0 -and $inputWait.ElapsedMilliseconds -lt 1800) {
            [Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 10
        }
        $record.outside_text = $inputBox.Text
        $record.outside_action = (Control "Run_$rowId").Current.Name
        if ($record.outside_text -ne 'a' -or $record.outside_action -ne '开始') { throw "External text '$($record.outside_text)', script action '$($record.outside_action)'" }
    } finally { $other.Dispose() }
    Send-Owned (Control 'NewScript') 'a'
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '停止' } 'A started script'
    Click-Id "Run_$rowId"
    Observe 'A runs the script in the app, types normally in a separate text window, and survives outside-click dismissal.'
    (Control 'EditScripts').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    $null = Row-Control 'Edit_'
    Click-Id "Edit_$rowId"
    $null = Wait-For { (Read-Value 'ScriptSource') -match 'user-shell' } 'loaded source'
    Set-Value 'ScriptSource' 'return 77'
    Send-Owned (Control 'ScriptSource') 'a'
    if ((Read-Value 'ScriptSource') -notmatch 'a') { throw 'Shortcut intercepted text inside editor' }
    Click-Id 'BackScripts'
    Click-Name '放弃修改'
    Click-Id "Edit_$rowId"
    if ((Read-Value 'ScriptSource') -notmatch 'user-shell') { throw 'Discard modified stored source' }
    Set-Value 'ScriptSource' 'return 42'
    Click-Id 'SaveScript'
    $null = Control 'NewScript'
    Navigate-To 'NavGroups'
    $null = Control 'NewGroup'
    Click-Id 'NewGroup'
    Set-Value 'GroupName' '测试目标组'
    Click-Name '保存'
    $null = Wait-For { Find-Control '测试目标组 · 0 个目标' -ByName } 'group created'
    Click-Id 'RenameButton'
    Set-Value 'GroupName' '游戏窗口'
    Click-Name '保存'
    $null = Wait-For { Find-Control '游戏窗口 · 0 个目标' -ByName } 'group renamed'
    Screenshot 'groups'
    Click-Id 'DeleteButton'
    Click-Id 'PrimaryButton'
    $null = Wait-For { Find-Control '全部目标 · 0 个目标' -ByName } 'group deleted'
    Navigate-To 'NavLogs'
    $null = Wait-For { $el=Find-Control 'LogEntries'; $el -and $el.Current.Name -match 'user-shell|running|cancelled' } 'execution logs'
    Screenshot 'logs'
    Navigate-To 'NavAbout'
    $null = Control 'CheckUpdates'
    foreach ($name in '欢迎使用测试版本','打开项目主页','打开问题反馈','打开更新日志') { $null = Wait-For { Find-Control $name -ByName } $name }
    Screenshot 'about'
    Click-Id 'CheckUpdates'
    $message = Wait-For { $s=(Control 'UpdateResult').Current.Name; if ($s -and $s -notmatch '正在') { $s } } 'update check result'
    $record.update_result = $message
    Observe 'Loaded saved source, discarded changes, saved edits, opened the groups, history and about pages.'
    Close-Client
    Start-Client
    $key = Control "Key_$rowId"
    if ($key.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0].Current.Name -ne 'A') { throw 'Hotkey did not persist' }
    (Control 'EditScripts').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    Click-Id "Edit_$rowId"
    if ((Read-Value 'ScriptSource') -ne 'return 42') { throw 'Saved source did not persist' }
    Click-Id 'BackScripts'
    Click-Id "Delete_$rowId"
    $null = Wait-For { -not (Find-Control 'EditScripts') } 'direct deletion and empty list'
    Close-Client
    Observe 'Restart preserved source and hotkey. Direct delete restored the empty library.'
    $record.status = 'PASS'
} catch {
    $record.status='FAIL'
    $record.error=$_.Exception.Message
    if ($root -and $app -and -not $app.HasExited) {
        $record.tree = @($root.FindAll($scope,[Windows.Automation.Condition]::TrueCondition) | ForEach-Object { "$($_.Current.AutomationId)  $($_.Current.Name)" } | Select-Object -First 100)
        try { Screenshot 'failure' } catch {}
    }
    throw
} finally {
    if ($app -and -not $app.HasExited) { $app.Kill(); [void]$app.WaitForExit(5000) }
    $record.finished = (Get-Date).ToString('o')
    $record | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 -LiteralPath (Join-Path $folder.FullName 'window.json')
}
