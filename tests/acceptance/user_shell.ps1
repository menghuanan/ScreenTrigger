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
if ((Test-Path (Join-Path $exeFile.DirectoryName 'resources/manifest/ScreenTrigger.portable.json')) -or
    (Test-Path (Join-Path $exeFile.DirectoryName 'ScreenTrigger.portable.json'))) {
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
function Selected-Name($id) { (Control $id).GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0].Current.Name }
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
function Open-Keys($key) {
    Send-Owned $key '%{DOWN}'
    $list = Control 'HotkeyChoices'
    $item = Wait-For { $list.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0] } 'current hotkey option'
    return $item
}
function Click-Owned($el) {
    $b = $el.Current.BoundingRectangle
    $point = [Drawing.Point]::new([int]($b.Left + $b.Width / 2),[int]($b.Top + $b.Height / 2))
    $owner = 0
    [void][UserShellProbe]::GetWindowThreadProcessId([UserShellProbe]::At($point.X,$point.Y),[ref]$owner)
    if ($owner -ne $app.Id) { throw 'Click destination does not belong to the test window' }
    $previous = [Windows.Forms.Cursor]::Position
    try {
        [Windows.Forms.Cursor]::Position = $point
        [UserShellProbe]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
        [UserShellProbe]::mouse_event(4,0,0,0,[UIntPtr]::Zero)
    } finally { [Windows.Forms.Cursor]::Position = $previous }
}
function Capture-KeyList($key,$list) {
    # A windowed WinUI flyout is composed separately from the main HWND.
    $box = [Windows.Rect]::Union($key.Current.BoundingRectangle,$list.Current.BoundingRectangle)
    $bitmap = [Drawing.Bitmap]::new([int]$box.Width,[int]$box.Height)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try {
        foreach ($x in ($box.Left + 3), ($box.Right - 3)) {
            foreach ($y in ($box.Top + 4), ($box.Bottom - 4)) {
                $owner = 0
                [void][UserShellProbe]::GetWindowThreadProcessId([UserShellProbe]::At([int]$x,[int]$y),[ref]$owner)
                if ($owner -ne $app.Id) { throw 'Another window overlaps the hotkey picker' }
            }
        }
        $graphics.CopyFromScreen([int]$box.X,[int]$box.Y,0,0,$bitmap.Size)
        $bitmap.Save((Join-Path $folder.FullName 'hotkeys-open.png'),[Drawing.Imaging.ImageFormat]::Png)
    } finally { $graphics.Dispose(); $bitmap.Dispose() }
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
    foreach ($id in 'NavScripts','NavGroups','NavLogs','NavAbout','NavSettings') { $null = Control $id }
    $left = (Control 'NewScript').Current.BoundingRectangle.Left
    $record.content_left = $left - $root.Current.BoundingRectangle.Left
    $scale = $record.dpi / 96
    Click-Id 'TogglePaneButton'
    $null = Wait-For { $left - (Control 'NewScript').Current.BoundingRectangle.Left -gt 100 * $scale } 'compact icon rail'
    Navigate-To 'NavSettings'
    $null = Wait-For { -not (Find-Control 'NewScript') } 'settings page'
    if ((Find-Control 'CheckUpdates') -or (Find-Control 'GroupFilter') -or (Find-Control 'RefreshLogs')) { throw 'Settings displays another page' }
    if ($root.Current.BoundingRectangle.Bottom - (Control 'NavSettings').Current.BoundingRectangle.Bottom -gt 40 * $scale) { throw 'Settings is not at the bottom' }
    Screenshot 'settings-compact'
    Click-Id 'TogglePaneButton'
    Navigate-To 'NavScripts'
    $null = Wait-For { [Math]::Abs((Control 'NewScript').Current.BoundingRectangle.Left - $left) -lt 2 * $scale } 'expanded navigation'
    Observe 'Top navigation toggle switches between labels and icons. Bottom Settings opens an empty page.'
    if (Find-Control 'EditScripts') { throw 'Edit visible with empty library' }
    Screenshot 'scripts-empty'
    Observe 'Fresh library is empty. New is enabled and Edit is hidden.'
    Click-Id 'NewScript'
    Set-Value 'ScriptName' '窗口测试'
    Set-Value 'ScriptSource' "log('user-shell'); wait(4); log('still-running'); wait(20)"
    Click-Id 'BackScripts'
    Click-Name '继续编辑'
    $null = Wait-For { -not (Find-Control '继续编辑' -ByName) } 'closed draft dialog'
    $null = Wait-For { (Read-Value 'ScriptName') -eq '窗口测试' } 'preserved draft'
    Click-Id 'BackScripts'
    Click-Name '保存并退出'
    $run = Row-Control 'Run_'
    $rowId = $run.Current.AutomationId.Substring(4)
    $key = Control "Key_$rowId"
    $option = Open-Keys $key
    $bounds = Wait-For {
        $box = (Control 'HotkeyChoices').Current.BoundingRectangle
        if ($box.Top -ge $key.Current.BoundingRectangle.Bottom - $scale) { $box }
    } 'expanded list below its trigger'
    $record.hotkey_bounds = @($key.Current.BoundingRectangle.Bottom, $bounds.Top)
    Send-Owned $option '{END}{ENTER}'
    $null = Wait-For { $key.Current.Name -eq '脚本热键，F12' } 'F12 bound'
    Click-Owned $key
    $list = Control 'HotkeyChoices'
    $option = Wait-For { $list.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()[0] } 'F12 in open flyout'
    Start-Sleep -Milliseconds 300
    if ($list.Current.BoundingRectangle.Top -lt $key.Current.BoundingRectangle.Bottom - $scale) { throw 'Bound hotkey flyout opened over its button' }
    Capture-KeyList $key $list
    Send-Owned $option '{UP}{ESC}'
    Screenshot 'scripts'
    Observe 'Created through save-on-exit. F12 selected, then dropdown dismissed without changing the selection.'
    Click-Control $run
    Navigate-To 'NavLogs'
    $null = Wait-For { (Selected-Name 'ExecutionRuns') -match '窗口测试 · 运行中' -and
                        (Control 'LogEntries').Current.Name -match 'user-shell' } 'named live run'
    $record.live_first = (Control 'LogEntries').Current.Name
    if ($record.live_first -match 'still-running') { throw 'First sample missed the interval between Lua log entries' }
    $null = Wait-For { (Control 'LogEntries').Current.Name -match 'still-running' } 'second Lua entry without Refresh'
    $record.live_run = Selected-Name 'ExecutionRuns'
    if ($record.live_run -notmatch '窗口测试 · 运行中') { throw 'Second entry appeared only after the run ended' }
    Screenshot 'logs-running'
    Send-Owned (Control 'RefreshLogs') '{F12}'
    $null = Wait-For { (Selected-Name 'ExecutionRuns') -match '窗口测试 · 已停止' } 'automatic terminal status'
    Navigate-To 'NavScripts'
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '开始' } 'stopped by F12'
    Send-Owned (Control 'NewScript') '{F12}'
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '停止' } 'started by F12'
    Click-Id "Run_$rowId"
    $null = Wait-For { (Control "Run_$rowId").Current.Name -eq '开始' } 'stopped by click'
    Observe 'Button and window hotkey both start and stop the running Lua script.'
    $key = Control "Key_$rowId"
    Send-Owned (Open-Keys $key) '{HOME}{DOWN}{ENTER}'
    $null = Wait-For { $key.Current.Name -eq '脚本热键，A' } 'A binding'
    Send-Owned (Open-Keys $key) '{DOWN}'
    Click-Owned (Control 'NewScript')
    if (Find-Control 'BackScripts') { Click-Id 'BackScripts' }
    $null = Wait-For { (Control "Key_$rowId").Current.Name -eq '脚本热键，A' } 'outside click retained A'
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
    $editRect = (Row-Control 'Edit_').Current.BoundingRectangle
    if ($editRect.Right -gt (Control "Delete_$rowId").Current.BoundingRectangle.Left) { throw 'Edit must precede Delete' }
    Screenshot 'scripts-edit'
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
    $null = Wait-For {
        $filter = (Control 'GroupFilter').Current.BoundingRectangle
        # WinUI includes the focus margin on both sides of the 260-DIP picker in its UIA bounds.
        $paintedLeft = $filter.Left + ($filter.Width - 260 * $scale) / 2
        [Math]::Abs($paintedLeft - $root.Current.BoundingRectangle.Left - $record.content_left) -le 2 * $scale
    } 'group filter aligned with the left edge of the target card'
    if ((Find-Control 'Count') -or (Find-Control 'MoveButton')) { throw 'Old group toolbar is visible' }
    Click-Id 'NewGroup'
    Set-Value 'GroupName' '测试目标组'
    Click-Name '保存'
    $null = Wait-For { (Selected-Name 'GroupFilter') -eq '测试目标组' } 'group created'
    Click-Id 'RenameButton'
    Set-Value 'GroupName' '游戏窗口'
    Click-Name '保存'
    $null = Wait-For { (Selected-Name 'GroupFilter') -eq '游戏窗口' } 'group renamed'
    Screenshot 'groups'
    Click-Id 'DeleteButton'
    Click-Id 'PrimaryButton'
    $null = Wait-For { (Selected-Name 'GroupFilter') -eq '全部目标' } 'group deleted'
    Navigate-To 'NavLogs'
    $null = Wait-For { $el=Find-Control 'LogEntries'; $el -and $el.Current.Name -match 'user-shell|running|cancelled' } 'execution logs'
    Screenshot 'logs'
    Navigate-To 'NavAbout'
    $null = Wait-For {
        $update = (Control 'CheckUpdates').Current.BoundingRectangle
        $window = $root.Current.BoundingRectangle
        [Math]::Abs($update.Left - $window.Left - $record.content_left) -le 2 * $scale -and
            $window.Bottom - $update.Bottom -le 40 * $scale
    } 'Check Updates anchored to the lower left'
    $titles = $root.FindAll($scope,[Windows.Automation.PropertyCondition]::new($ae::NameProperty,'关于'))
    $contentStart = $root.Current.BoundingRectangle.Left + $record.content_left
    if ($titles | Where-Object { $_.Current.BoundingRectangle.Left -ge $contentStart }) { throw 'Duplicate About heading' }
    foreach ($name in '欢迎使用测试版本','打开项目主页','打开问题反馈','打开更新日志') { $null = Wait-For { Find-Control $name -ByName } $name }
    Screenshot 'about'
    Click-Id 'CheckUpdates'
    $message = Wait-For { $s=(Control 'UpdateResult').Current.Name; if ($s -and $s -notmatch '正在') { $s } } 'update check result'
    $record.update_result = $message
    Observe 'Loaded saved source, discarded changes, saved edits, opened the groups, history and about pages.'
    Close-Client
    Start-Client
    $key = Control "Key_$rowId"
    if ($key.Current.Name -ne '脚本热键，A') { throw 'Hotkey did not persist' }
    (Control 'EditScripts').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    Click-Id "Edit_$rowId"
    if ((Read-Value 'ScriptSource') -ne 'return 42') { throw 'Saved source did not persist' }
    Click-Id 'BackScripts'
    Click-Id "Delete_$rowId"
    $null = Wait-For { -not (Find-Control 'EditScripts') } 'direct deletion and empty list'
    Navigate-To 'NavLogs'
    $null = Wait-For { (Selected-Name 'ExecutionRuns') -match '窗口测试 · 已停止' } 'deleted script retains its name in history'
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
