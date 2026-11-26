# Invoked by verify-workspace.ps1 with its owned product window and exclusive output directory.
function Pick-TestTextOutput([string]$Path) {
    Invoke-Element PickTextOutput
    $dialog = Wait-Until {
        $win = [Windows.Automation.AutomationElement]::FromHandle([OwnedWindow]::GetForegroundWindow())
        if ($win.Current.ProcessId -eq $app.Id -and $win.Current.Name -match '写入的 TXT') { $win }
    } 'owned foreground text output picker'
    $handle = [IntPtr]$dialog.Current.NativeWindowHandle
    Wait-Until { [OwnedWindow]::FinishTextPicker($handle, $Path) } 'native filename and save button ready' | Out-Null
    Wait-Until { !(File-Dialog) } 'picker closed' | Out-Null
}

function Test-TextOutput {
    Add-Type -AssemblyName System.Windows.Forms
    $target = Join-Path $folder.FullName 'screen-text.txt'
    Select-Element (Element NavMonitor)
    Pick-TestTextOutput $target
    $observations.monitorFile = Wait-Text TextOutputStatus '追加.*screen-text.txt'
    $mode = Element OutputMode
    $mode.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    $overwrite = Wait-Until { $mode.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($ae::NameProperty,'覆盖文件'))) } 'overwrite mode'
    Select-Element $overwrite
    Wait-Text TextOutputStatus '未选择' | Out-Null
    Pick-TestTextOutput $target
    $observations.monitorOverwrite = Wait-Text TextOutputStatus '覆盖.*screen-text.txt'
    Invoke-Element ClearTextOutput
    Wait-Text TextOutputStatus '未选择' | Out-Null
    if (Test-Path -LiteralPath $target) { throw 'Authorizing or clearing an output created a file' }
    Select-Element (Element NavScripts)
    Set-Value ScriptName '文字导出前台窗口'
    $display = [Windows.Forms.Screen]::PrimaryScreen.DeviceName
    $script = "local words=ocr(nil,nil,'PP-OCRv6_medium_rec'); return #words"
    Set-Value ScriptSource $script
    Invoke-Element SaveScript
    $observations.script = Wait-Text ScriptFeedback '修订 1'
    Select-Element (Element NavRun)
    $scope = Element Scope
    $scope.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    $region = @{space='screen_physical';display_id=$display;x=100;y=150;width=300;height=80} | ConvertTo-Json -Compress
    Set-Value Region $region
    $pick = Element PickTextOutput
    $pick.SetFocus()
    Wait-Until { $pick.Current.HasKeyboardFocus } 'keyboard focus on file selection' | Out-Null
    Pick-TestTextOutput $target
    $observations.selected = Wait-Text TextOutputStatus '追加.*screen-text.txt'
    Pick-TestTextOutput ''
    $observations.afterCancel = Wait-Text TextOutputStatus '追加.*screen-text.txt'
    if (Test-Path -LiteralPath $target) { throw 'Picker altered a file before OCR' }
    Save-Tree 'prepared'
    $bitmap = New-Object Drawing.Bitmap(300,80)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $font = New-Object Drawing.Font('Arial',36,[Drawing.FontStyle]::Bold,[Drawing.GraphicsUnit]::Pixel)
    try {
        $graphics.Clear([Drawing.Color]::White)
        $graphics.DrawString('HELLO', $font, [Drawing.Brushes]::Black, 24, 16)
        $bitmap.Save((Join-Path $folder.FullName 'fixture.png'), [Drawing.Imaging.ImageFormat]::Png)
    } finally { $font.Dispose(); $graphics.Dispose(); $bitmap.Dispose() }
    $py = (Resolve-Path '.venv/Scripts/python.exe').Path
    $fixture = Start-Process -FilePath $py -ArgumentList @('-X','utf8','tests/acceptance/show_fixture.py',('"'+$folder.FullName+'"')) -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $folder.FullName 'fixture.log')
    try {
        Wait-Until { Test-Path (Join-Path $folder.FullName 'fixture-visible.json') } 'owned HELLO window visible' | Out-Null
        Invoke-Element ExecuteScript
        $observations.completed = Wait-Text MonitorObservation '已完成'
        $observations.bytesFeedback = Wait-Text TextWriteStatus '已写入 7 字节'
        $bytes = [IO.File]::ReadAllBytes($target)
        if ([BitConverter]::ToString($bytes) -ne '48-45-4C-4C-4F-0D-0A') { throw 'Expected HELLO and CRLF' }
        $observations.outputSha256 = (Get-FileHash $target).Hash
        Save-Tree 'exported'
    } finally {
        [IO.File]::WriteAllText((Join-Path $folder.FullName 'fixture-stop'), 'done')
        if (!$fixture.WaitForExit(6000)) { $fixture.Kill() }
    }
}
