# Run against a fresh, disposable extraction or an isolated Debug data root.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Exe, [Parameter(Mandatory)][string]$Output, [switch]$WorkspaceOnly)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,System.Windows.Forms,System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class UserShellProbe {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hwnd, uint msg, IntPtr wparam, IntPtr lparam);
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
        [Windows.Forms.Application]::DoEvents()
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
function Set-Expanded($id, [bool]$expanded) {
    $pattern = (Control $id).GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
    if ($expanded) { $pattern.Expand() } else { $pattern.Collapse() }
}
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
    $null = Wait-For { $b = Find-Control 'RefreshWindows'; $b -and $b.Current.IsEnabled } 'ready work controls'
}
function Close-Client {
    $root.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (-not $app.WaitForExit(15000)) { throw 'Application did not close' }
    if ($app.ExitCode -ne 0) { throw "Application exited $($app.ExitCode)" }
}
function Select-Option($id, $prefix) {
    $box = Control $id
    $item = Wait-For {
        if (-not $box.Current.IsEnabled) { return }
        $expand = $box.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
        if ($expand.Current.ExpandCollapseState -eq 'Collapsed') { $expand.Expand() }
        $root.FindAll($scope,[Windows.Automation.PropertyCondition]::new($ae::ControlTypeProperty,[Windows.Automation.ControlType]::ListItem)) |
            Where-Object { $_.Current.Name.StartsWith($prefix) } | Select-Object -First 1
    } "choice $prefix"
    $item.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
    $box.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Collapse()
}
function Confirm-FixtureSelection($fixture) {
    $overlay = Wait-For {
        $ae::RootElement.FindAll([Windows.Automation.TreeScope]::Children,[Windows.Automation.PropertyCondition]::new($ae::ProcessIdProperty,$app.Id)) |
            Where-Object { $_.Current.Name -match '选择区域' } | Select-Object -First 1
    } 'product selection overlay'
    $from = $fixture.PointToScreen([Drawing.Point]::new(40,40))
    $to = $fixture.PointToScreen([Drawing.Point]::new(140,110))
    foreach ($p in $from,$to) {
        $owner = 0
        [void][UserShellProbe]::GetWindowThreadProcessId([UserShellProbe]::At($p.X,$p.Y),[ref]$owner)
        if ($owner -ne $app.Id) { throw 'Overlay does not own drag endpoints' }
    }
    [Windows.Forms.Cursor]::Position = $from
    Start-Sleep -Milliseconds 180
    [UserShellProbe]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
    try { Start-Sleep -Milliseconds 120; [Windows.Forms.Cursor]::Position = $to; Start-Sleep -Milliseconds 180 }
    finally { [UserShellProbe]::mouse_event(4,0,0,0,[UIntPtr]::Zero) }
    $ok = Wait-For { $el=$overlay.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'ConfirmSelection'))
        if ($el -and $el.Current.IsEnabled) { $el } } 'live preview enables confirmation'
    $readout = $overlay.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'Coordinates')).Current.Name
    if ($readout -notmatch '\((-?\d+), (-?\d+)\)\s+(\d+) × (\d+) px') { throw "Missing physical selection readout: $readout" }
    $origin = $fixture.PointToScreen([Drawing.Point]::Empty)
    $x = [int]$Matches[1] - $origin.X; $y = [int]$Matches[2] - $origin.Y
    $width = [int]$Matches[3]; $height = [int]$Matches[4]
    if ($x -lt 0 -or $y -lt 0 -or $x + $width -gt 260 -or $y + $height -gt 200 -or $width -lt 10 -or $height -lt 10) { throw 'Selection left the fixture' }
    Click-Control $ok
    return [PSCustomObject]@{ X=$x; Y=$y; Width=$width; Height=$height; Readout=$readout }
}
function Test-RegionRecording($dataRoot,$scriptId) {
    $cursor = [Windows.Forms.Cursor]::Position
    $state = [hashtable]::Synchronized(@{})
    $fixtureRunspace = [RunspaceFactory]::CreateRunspace()
    $fixtureRunspace.ApartmentState = 'STA'
    $fixtureRunspace.Open()
    $worker = [PowerShell]::Create()
    $worker.Runspace = $fixtureRunspace
    $null = $worker.AddScript({
        param($state, $title)
        $form = [Windows.Forms.Form]::new()
        try {
            $form.Text = $title
            $form.StartPosition = 'Manual'
            $desktop = [Windows.Forms.Screen]::PrimaryScreen.WorkingArea
            $form.Location = [Drawing.Point]::new(($desktop.Left + 30),($desktop.Top + 160))
            $form.ClientSize = [Drawing.Size]::new(260,200)
            $form.BackColor = [Drawing.Color]::CornflowerBlue
            $form.TopMost = $true
            $form.add_Shown({ $state.Form = $form })
            [Windows.Forms.Application]::Run($form)
        } finally { $form.Dispose() }
    }).AddArgument($state).AddArgument("ScreenTrigger fixture $PID")
    $result = $worker.BeginInvoke()
    try {
        $fixture = Wait-For { if ($result.IsCompleted) { throw "Fixture ended: $($worker.Streams.Error)" }; $state.Form } 'responsive test window'
        Navigate-To 'NavWork'
        $null = Wait-For { $button=Find-Control 'RefreshWindows'; $button -and $button.Current.IsEnabled } 'work page ready'
        [void][UserShellProbe]::SetForegroundWindow($fixture.Handle)
        Click-Id 'RefreshWindows'
        Select-Option 'WorkScope' $fixture.Text
        $null = Wait-For { (Control 'WorkBackground').Current.IsEnabled } 'window permits background mode'
        Click-Id 'PickWorkRegion'
        $selected = Confirm-FixtureSelection $fixture
        $x = $selected.X; $y = $selected.Y; $width = $selected.Width; $height = $selected.Height
        $readout = $selected.Readout
        $null = Wait-For { Find-Control "已选区域　($x, $y)　$width × $height px" -ByName } 'window-client rectangle matches the physical overlay'
        (Control 'WorkBackground').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
        Select-Option 'WorkMode' '单次'
        Screenshot 'work-region'
        $history = Join-Path $dataRoot 'history'
        $priorRuns = @(Get-ChildItem $history -Filter '*.json' -Recurse | ForEach-Object FullName)
        Click-Id 'StartWork'
        $null = Wait-For {
            Get-ChildItem $history -Filter '*.json' -Recurse | Where-Object { $_.FullName -notin $priorRuns } |
                ForEach-Object {
                    $run = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($run.script_id -eq $scriptId -and $run.iteration -eq 1 -and $run.status -eq 'completed') { $run }
                }
        } 'completed one background run in the selected window region'
        Navigate-To 'NavScripts'
        Click-Id 'RecordScript'
        Click-Name '开始录制'
        $null = Wait-For { (Control 'FinishRecording').Current.IsEnabled } 'recording armed'
        $inside = $fixture.PointToScreen([Drawing.Point]::new(($x + [int]($width / 2)),($y + [int]($height / 2))))
        $null = Wait-For {
            [void][UserShellProbe]::SetWindowPos($fixture.Handle,[IntPtr](-1),0,0,0,0,0x43)
            [void][UserShellProbe]::SetForegroundWindow($fixture.Handle)
            [UserShellProbe]::GetAncestor([UserShellProbe]::At($inside.X,$inside.Y),2) -eq $fixture.Handle
        } 'recording fixture visible after the scope dialog closed'
        [Windows.Forms.Cursor]::Position = $inside
        Start-Sleep -Milliseconds 150
        if ([UserShellProbe]::GetAncestor([UserShellProbe]::At($inside.X,$inside.Y),2) -ne $fixture.Handle) { throw 'Fixture moved before recording' }
        [UserShellProbe]::mouse_event(2,0,0,0,[UIntPtr]::Zero)
        try {
            $null = Wait-For { [UserShellProbe]::GetForegroundWindow() -eq $fixture.Handle } 'verified fixture gained foreground on click'
            Start-Sleep -Milliseconds 100
        } finally { [UserShellProbe]::mouse_event(4,0,0,0,[UIntPtr]::Zero) }
        [Windows.Forms.Application]::DoEvents()
        $null = Wait-For { Find-Control '正在录制 · 1 个动作' -ByName } 'one recorded fixture click'
        Click-Id 'FinishRecording'
        $null = Wait-For { (Read-Value 'ScriptSource') -match 'click_anchor' } 'recorded click compiled to editable Lua'
        Screenshot 'recording'
        Click-Id 'BackScripts'
        Click-Name '放弃修改'
        Navigate-To 'NavWork'
        Select-Option 'WorkScope' '显示器'
        Navigate-To 'NavGroups'
        Click-Id 'SelectTargetImage'
        $captured = Confirm-FixtureSelection $fixture
        $null = Wait-For { Find-Control "$($captured.Width) × $($captured.Height) px" -ByName } 'captured target preview'
        Screenshot 'target-capture'
        Navigate-To 'NavScripts'
        Click-Name '放弃修改'
        $null = Control 'NewScript'
        Observe 'Completed one background run in the selected window region and previewed a target captured from the fixture.'
        Observe "Confirmed physical selection $readout, checked its window-client offset and compiled one real in-scope fixture click into a Lua draft."
    } finally {
        if ($state.Form) { [void][UserShellProbe]::PostMessage($state.Form.Handle,16,[IntPtr]::Zero,[IntPtr]::Zero) }
        if (-not $result.AsyncWaitHandle.WaitOne(3000)) { $worker.Stop() }
        $worker.Dispose()
        $fixtureRunspace.Dispose()
        [Windows.Forms.Cursor]::Position = $cursor
    }
}
try {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    Start-Client
    $record.ready_ms = $clock.ElapsedMilliseconds
    $record.dpi = [UserShellProbe]::GetDpiForWindow($app.MainWindowHandle)
    $record.display = @([Windows.Forms.Screen]::AllScreens | ForEach-Object { "$($_.DeviceName) $($_.Bounds)" })
    Screenshot 'work-empty'
    if ((Control 'StartWork').Current.IsEnabled) { throw 'Empty library permits starting work' }
    Navigate-To 'NavScripts'
    foreach ($id in 'NavWork','NavScripts','NavGroups','NavLogs','NavAbout','NavSettings') { $null = Control $id }
    $left = (Control 'NewScript').Current.BoundingRectangle.Left
    $record.content_left = $left - $root.Current.BoundingRectangle.Left
    $scale = $record.dpi / 96
    if (-not $WorkspaceOnly) {
        Click-Id 'TogglePaneButton'
        $null = Wait-For { $left - (Control 'NewScript').Current.BoundingRectangle.Left -gt 100 * $scale } 'compact icon rail'
        Navigate-To 'NavSettings'
        $null = Control 'SettingsTitle'
        $null = Wait-For { (Control 'EngineHealth').Current.Name -eq '引擎就绪' } 'engine ready in Settings'
        if ((Find-Control 'CheckUpdates') -or (Find-Control 'GroupFilter') -or (Find-Control 'RefreshLogs')) { throw 'Settings displays another page' }
        if ($root.Current.BoundingRectangle.Bottom - (Control 'NavSettings').Current.BoundingRectangle.Bottom -gt 40 * $scale) { throw 'Settings is not at the bottom' }
        Click-Id 'TogglePaneButton'
        $null = Wait-For {
            $ocr = (Control 'OcrModelLibrary').Current.BoundingRectangle
            $backup = (Control 'CatalogBackups').Current.BoundingRectangle
            $ocr.Width -gt 0 -and $backup.Width -gt 0 -and $backup.Left -gt $ocr.Left -and [Math]::Abs($ocr.Top - $backup.Top) -lt 2 * $scale
        } 'OCR left and backups right on the same row'
        $null = Wait-For {
            [Math]::Abs((Control 'StopEngine').Current.BoundingRectangle.Top - (Control 'EngineHealth').Current.BoundingRectangle.Top) -lt 24 * $scale
        } 'engine controls aligned beside the summary at full width'
        Screenshot 'settings-wide'
        $window = $root.GetCurrentPattern([Windows.Automation.TransformPattern]::Pattern)
        $window.Resize(660 * $scale, 540 * $scale)
        $null = Wait-For {
            $ocr = (Control 'OcrModelLibrary').Current.BoundingRectangle
            $backup = (Control 'CatalogBackups').Current.BoundingRectangle
            $ocr.Width -gt 0 -and $backup.Width -gt 0 -and $backup.Top -gt $ocr.Top -and [Math]::Abs($ocr.Left - $backup.Left) -lt 2 * $scale
        } 'stacked settings cards in a narrow window'
        Screenshot 'settings-narrow'
        $window.Resize(1060 * $scale, 720 * $scale)
        Click-Id 'Diagnostics'
        $null = Wait-For { (Control 'EngineDiagnostics').Current.Name -match '配置修订' } 'inline engine diagnostics'
        Click-Id 'RefreshDiagnostics'
        $null = Wait-For { (Control 'EngineDiagnostics').Current.Name -match '进程树托管' } 'refreshed diagnostics'
        Screenshot 'settings-diagnostics'
        Click-Id 'Diagnostics'
        Set-Expanded 'EngineCapabilitiesPanel' $true
        $null = Control 'Capability_ocr'
        $null = Control 'Capability_lua_script'
        if ((Control 'InspectModels').Current.IsEnabled) {
            Click-Id 'InspectModels'
            $null = Wait-For {
                $list = Control 'InstalledModels'
                $list.Current.BoundingRectangle.Height -gt 0 -and $list.Current.Name -match '语言|OCR 模型'
            } 'visible OCR model manifests'
            Screenshot 'settings-capabilities'
            Click-Id 'InspectModels'
            Observe 'Engine capabilities list OCR and Lua. The installed-model inspection opens and closes.'
        } else { Observe 'Engine capabilities list OCR and Lua. Model inspection is disabled when OCR is unavailable.' }
        Set-Expanded 'EngineCapabilitiesPanel' $false
        Set-Expanded 'OcrModelLibrary' $true
        $null = Control 'OcrPipelines'
        $null = Control 'ImportOcrModel'
        $null = Control 'RemoveOcrModel'
        Click-Name '刷新状态'
        $null = Wait-For { (Control 'ModelInfo').Current.Name -match 'PP-OCR' } 'offline OCR model status'
        Set-Expanded 'OcrModelLibrary' $false
        $fields = [ordered]@{
            general = 'language','theme'; recognition = 'default_frequency_hz','default_threshold','rgb_tolerance_r','rgb_tolerance_g','rgb_tolerance_b','hsv_tolerance_h','hsv_tolerance_s','hsv_tolerance_v','ocr_model_id','ocr_min_confidence','model_directory'
            capture = 'preview_hz','png_compression_level'; input = 'allow_exit','move_duration_ms','type_interval_ms'; execution = 'total_timeout_ms'
            hotkeys = 'start_selection','stop_script','pause_script'; logging = 'level','rotate_bytes','file_count'
        }
        foreach ($group in $fields.Keys) {
            Set-Expanded "SettingGroup_$group" $true
            foreach ($field in $fields[$group]) { $null = Control "Setting_${group}_$field" }
            Set-Expanded "SettingGroup_$group" $false
        }
        Set-Expanded 'SettingGroup_recognition' $true
        $field = 'Setting_recognition_model_directory'
        $original = Read-Value $field
        Set-Value $field 'models-draft'
        Navigate-To 'NavAbout'
        $null = Wait-For { Find-Control '保存设置的修改？' -ByName } 'settings draft prompt'
        Send-Owned (Control 'CloseButton') '{ESC}'
        $null = Wait-For { -not (Find-Control '保存设置的修改？' -ByName) } 'settings draft dialog closed'
        $null = Wait-For { (Read-Value $field) -eq 'models-draft' } 'cancel navigation preserves draft'
        Click-Id 'CancelSettings'
        Set-Expanded 'SettingGroup_recognition' $true
        if ((Read-Value $field) -ne $original) { throw 'Undo did not restore the model directory' }
        Set-Value $field 'models-settings-test'
        Click-Id 'SaveSettings'
        $null = Wait-For { (Control 'SettingsFeedback').Current.Name -match '设置已保存' } 'saved settings'
        Click-Id 'ReloadSettings'
        Set-Expanded 'SettingGroup_recognition' $true
        if ((Read-Value $field) -ne 'models-settings-test') { throw 'Reload did not read the saved setting' }
        Click-Id 'ResetSettings'
        $null = Wait-For { Find-Control '恢复默认设置？' -ByName } 'reset preview'
        Send-Owned (Control 'PrimaryButton') '{ENTER}'
        $null = Wait-For { (Control 'SettingsFeedback').Current.Name -match '已恢复默认设置' } 'reset settings'
        Click-Id 'StopEngine'
        $null = Wait-For { (Control 'StartEngine').Current.IsEnabled -and -not (Find-Control 'SaveSettings') } 'stopped engine and disabled configuration'
        Click-Id 'StartEngine'
        $null = Wait-For { (Control 'StopEngine').Current.IsEnabled -and (Find-Control 'SaveSettings') } 'engine restart with fresh configuration'
        Navigate-To 'NavScripts'
        $null = Wait-For { [Math]::Abs((Control 'NewScript').Current.BoundingRectangle.Left - $left) -lt 2 * $scale } 'expanded navigation'
        Observe 'Bottom Settings preserves the shell navigation. OCR and backups are side by side at full width and stack when narrow.'
        Observe 'All settings fields open. Draft cancellation, undo, save, reload and reset work. Engine diagnostics refresh, and stop/start reloads the settings session.'
    }
    Screenshot 'scripts-empty'
    Click-Id 'RecordScript'
    Click-Name '开始录制'
    $null = Control 'FinishRecording'
    Click-Id 'CancelRecording'
    $null = Wait-For { (Control 'RecordScript').Current.IsEnabled } 'recording cancelled'
    Click-Id 'NewScript'
    Set-Value 'ScriptName' '窗口测试'
    foreach ($label in '鼠标点击','鼠标移动','鼠标拖拽','鼠标滚动','键盘按键','输入文本','等待','等待目标','目标识别','点击目标','颜色识别','OCR 识别','OCR 导出') { $null = Control "AddStep_$label" }
    Click-Id 'AddStep_等待'
    Set-Value 'StepParameter0' '125'
    Click-Name '添加步骤'
    $null = Wait-For { Find-Control '1 步' -ByName } 'delay inserted'
    Click-Id 'AddStep_OCR 识别'
    $null = Wait-For { Find-Control '2 步' -ByName } 'OCR inserted'
    $null = Wait-For { $row = Find-Control '125 ms' -ByName; $row -and $row.Current.BoundingRectangle.Width -gt 0 } 'visible step parameter'
    Start-Sleep -Milliseconds 350
    Screenshot 'assembly'
    (Control 'ScriptMode').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    if ((Read-Value 'ScriptSource') -notmatch 'wait\(0\.125\)' -or (Read-Value 'ScriptSource') -notmatch 'ocr\(') { throw 'Assembly did not emit Lua' }
    $custom = "log('user-shell'); wait(4); log('still-running'); wait(20)"
    Set-Value 'ScriptSource' $custom
    (Control 'ScriptMode').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    (Control 'ScriptMode').GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern).Toggle()
    if ((Read-Value 'ScriptSource') -cne $custom) { throw 'Mode switch modified handwritten Lua' }
    Screenshot 'lua'
    Click-Id 'BackScripts'
    Click-Name '继续编辑'
    $null = Wait-For { -not (Find-Control '继续编辑' -ByName) } 'closed draft dialog'
    Click-Id 'BackScripts'
    Click-Name '保存并退出'
    $edit = Row-Control 'Edit_'
    $rowId = $edit.Current.AutomationId.Substring(5)
    if ((Find-Control "Run_$rowId") -or (Find-Control "Key_$rowId")) { throw 'Library retained a run or hotkey control' }
    Observe 'Recording cancellation, assembly palette, editable delay, Lua conversion, custom source round-trip and save-on-exit passed.'
    Navigate-To 'NavWork'
    $null = Wait-For { (Selected-Name 'WorkScript') -eq '窗口测试' } 'saved script in Work'
    Screenshot 'work-ready'
    Click-Id 'StartWork'
    $null = Wait-For { (Control 'StartWork').Current.Name -eq '停止' } 'accepted run'
    Navigate-To 'NavLogs'
    $null = Wait-For { (Selected-Name 'ExecutionRuns') -match '窗口测试 · 运行中' -and
                        (Control 'LogEntries').Current.Name -match 'user-shell' } 'named live run'
    $null = Wait-For { (Control 'LogEntries').Current.Name -match 'still-running' } 'updated live log'
    Screenshot 'logs-running'
    Navigate-To 'NavWork'
    Click-Id 'StartWork'
    $null = Wait-For { (Control 'WorkStatus').Current.Name -match '已停止' } 'stopped via Work'
    Observe 'Started a saved Lua script from Work, followed its live log and stopped it after returning from Logs.'
    Navigate-To 'NavScripts'
    Click-Id "Edit_$rowId"
    $null = Wait-For { (Read-Value 'ScriptSource') -eq $custom } 'loaded source'
    Set-Value 'ScriptSource' 'return 77'
    Click-Id 'BackScripts'
    Click-Name '放弃修改'
    Click-Id "Edit_$rowId"
    if ((Read-Value 'ScriptSource') -ne $custom) { throw 'Discard modified stored source' }
    Set-Value 'ScriptSource' 'return 42'
    Click-Id 'SaveScript'
    $null = Control 'NewScript'
    Navigate-To 'NavWork'
    Select-Option 'WorkMode' '指定次数'
    $count = Control 'WorkCount'
    $entry = $count.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'InputBox'))
    $entry.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).SetValue('3')
    Send-Owned $entry '{ENTER}'
    Click-Id 'StartWork'
    $null = Wait-For { (Control 'WorkStatus').Current.Name -match '已完成' } 'counted run completes'
    $dataRoot = if ($env:SIDECAR_DATA_DIR) { $env:SIDECAR_DATA_DIR } else { Join-Path $exeFile.DirectoryName 'data' }
    $null = Wait-For {
        Get-ChildItem -LiteralPath (Join-Path $dataRoot 'history') -Filter '*.json' -Recurse |
            ForEach-Object { $summary = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($summary.script_id -eq $rowId -and $summary.iteration -eq 3 -and $summary.status -eq 'completed') { $summary } }
    } 'three persisted iterations'
    Observe 'Verified the completed three-iteration run in the persisted execution history.'
    Test-RegionRecording $dataRoot $rowId
    Navigate-To 'NavSettings'
    Set-Expanded 'CatalogBackups' $true
    Click-Id 'CreateBackup'
    $null = Wait-For { (Control 'SettingsFeedback').Current.Name -match '备份已保存' } 'catalog backup created'
    Click-Id 'PreviewRestore'
    $null = Wait-For { Find-Control '恢复这些目标与脚本？' -ByName } 'backup restore preview'
    Send-Owned (Control 'CloseButton') '{ESC}'
    $null = Wait-For { (Control 'SettingsFeedback').Current.Name -match '已取消恢复' } 'backup restore cancelled'
    Click-Id 'PreviewRestore'
    $null = Wait-For { Find-Control '恢复这些目标与脚本？' -ByName } 'second backup restore preview'
    Send-Owned (Control 'PrimaryButton') '{ENTER}'
    $null = Wait-For { (Control 'SettingsFeedback').Current.Name -match '已恢复，当前 generation' } 'catalog backup restored'
    Observe 'User Settings creates a catalog backup and previews, cancels and confirms its restore.'
    Navigate-To 'NavGroups'
    $null = Control 'TargetTree'
    Click-Id 'NewGroup'
    Set-Value 'GroupName' '测试目标组'
    Click-Name '保存'
    $null = Wait-For { (Selected-Name 'TargetGroup') -eq '测试目标组' } 'group created'
    Click-Id 'RenameButton'
    Set-Value 'GroupName' '游戏窗口'
    Click-Name '保存'
    $null = Wait-For { (Selected-Name 'TargetGroup') -eq '游戏窗口' } 'group renamed'
    $samplePath = Join-Path $folder.FullName 'target.png'
    $bitmap = [Drawing.Bitmap]::new(40,30)
    try { $bitmap.SetPixel(7,5,[Drawing.Color]::Blue); $bitmap.Save($samplePath,[Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
    Add-Type -Path (Join-Path $PSScriptRoot '../../tools/probes/windows/PickerFixture.cs')
    Set-Value 'TargetName' '测试样本'
    Click-Id 'ImportTargetImage'
    $picker = Wait-For {
        $owned = $root.FindFirst($scope,[Windows.Automation.PropertyCondition]::new($ae::ClassNameProperty,'#32770'))
        if ($owned) { return $owned }
        $ae::RootElement.FindAll([Windows.Automation.TreeScope]::Children,[Windows.Automation.Condition]::TrueCondition) |
            Where-Object { $_.Current.ClassName -eq '#32770' -and [OwnedWindow]::GetWindow([IntPtr]$_.Current.NativeWindowHandle,4) -eq $app.MainWindowHandle } |
            Select-Object -First 1
    } 'owned file picker'
    $dialogHandle = [IntPtr]$picker.Current.NativeWindowHandle
    if (-not [OwnedWindow]::SetFilename($dialogHandle,$samplePath) -or
        -not [OwnedWindow]::PostMessage($dialogHandle,273,[IntPtr]1,[IntPtr]::Zero)) { throw 'Native target image picker failed' }
    $null = Wait-For { Find-Control '40 × 30 px' -ByName } 'decoded image preview'
    Click-Id 'SaveTarget'
    $null = Wait-For { Find-Control '#1　测试样本' -ByName } 'saved target in tree'
    Screenshot 'groups'
    Click-Id 'NewTarget'
    $null = Wait-For { (Read-Value 'TargetName') -eq '' } 'fresh target draft'
    $node = Wait-For { Find-Control '#1　测试样本' -ByName } 'saved target node'
    [void][UserShellProbe]::SetForegroundWindow($app.MainWindowHandle)
    Click-Owned $node
    $null = Wait-For { (Read-Value 'TargetName') -eq '测试样本' -and (Find-Control '40 × 30 px' -ByName) } 'reopened stored image preview'
    foreach ($destination in '未分组','游戏窗口') {
        Select-Option 'TargetGroup' $destination
        Click-Id 'SaveTarget'
        $null = Wait-For { Find-Control '已保存目标 #1　·　40 × 30 px' -ByName } 'move saved'
        Click-Id 'NewTarget'
        $null = Wait-For { (Read-Value 'TargetName') -eq '' } 'cleared target editor'
        Click-Owned (Wait-For { Find-Control '#1　测试样本' -ByName } 'moved target')
        $null = Wait-For { (Read-Value 'TargetName') -eq '测试样本' -and (Selected-Name 'TargetGroup') -eq $destination } 'persisted group move'
    }
    Observe 'Moved the saved target to Ungrouped and back to its named group, reopening it after each save.'
    Click-Id 'DeleteButton'
    Click-Id 'PrimaryButton'
    $null = Wait-For { (Selected-Name 'TargetGroup') -eq '未分组' } 'group deleted'
    Observe 'Created and renamed a group, imported a real PNG, saved the target and reopened its persistent preview.'
    Navigate-To 'NavLogs'
    $null = Wait-For { $el=Find-Control 'LogEntries'; $el -and $el.Current.Name -match 'user-shell|running|cancelled|completed' } 'execution logs'
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
    Navigate-To 'NavScripts'
    Click-Id "Edit_$rowId"
    if ((Read-Value 'ScriptSource') -ne 'return 42') { throw 'Saved source did not persist' }
    Click-Id 'BackScripts'
    Click-Id "Delete_$rowId"
    $null = Wait-For { -not (Find-Control "Edit_$rowId") } 'direct deletion and empty list'
    Navigate-To 'NavLogs'
    $null = Wait-For { (Selected-Name 'ExecutionRuns') -match '窗口测试 · 已完成' } 'deleted script retains its name in history'
    Close-Client
    Observe 'Restart preserved the saved Lua source. Direct delete restored the empty library.'
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
