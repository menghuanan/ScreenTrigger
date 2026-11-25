# Called by verify-workspace.ps1 with the verified product window and an exclusive data root.
Add-Type -Path (Join-Path $PSScriptRoot 'PickerFixture.cs')
Add-Type -AssemblyName System.Drawing
function Invoke-Named($text) {
    $control = Wait-Until { $e = Find-Element $text -Name; if ($e.Current.IsEnabled) { $e } } $text
    $control.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Wait-Text($id, $pattern) {
    Wait-Until { $e = Find-Element $id; if ($e.Current.Name -match $pattern) { $e.Current.Name } } $pattern
}
function File-Dialog {
    $owned = $main.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($ae::ClassNameProperty,'#32770')))
    if ($owned) { return $owned }
    foreach ($win in $ae::RootElement.FindAll([Windows.Automation.TreeScope]::Children,[Windows.Automation.Condition]::TrueCondition)) {
        if ($win.Current.ClassName -eq '#32770' -and
            ($win.Current.ProcessId -eq $app.Id -or [OwnedWindow]::GetWindow([IntPtr]$win.Current.NativeWindowHandle,4) -eq $hwnd)) { return $win }
    }
}
function Pick-Image($path) {
    Invoke-Element ImportImage
    $dialog = Wait-Until { File-Dialog } 'owned picker'
    $buttonId = if ($path) { '1' } else { '2' }
    $handle = [IntPtr]$dialog.Current.NativeWindowHandle
    if ($path -and ![OwnedWindow]::SetFilename($handle,$path)) { throw 'Native filename edit missing' }
    if (![OwnedWindow]::PostMessage($handle,273,[IntPtr]([int]$buttonId),[IntPtr]::Zero)) { throw 'Picker command failed' }
}
function Test-ImageWorkspace {
    $sample = Join-Path $folder.FullName 'fixture.png'
    $bitmap = New-Object Drawing.Bitmap(80,60)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.Clear([Drawing.Color]::CornflowerBlue)
        $graphics.FillEllipse([Drawing.Brushes]::Gold,8,10,38,29)
        $bitmap.Save($sample,[Drawing.Imaging.ImageFormat]::Png)
    } finally { $graphics.Dispose(); $bitmap.Dispose() }
    Select-Element (Element NavRun)
    Invoke-Element ExecuteScript
    $observations.run = Wait-Text RunStatus '已完成'
    Select-Element (Element NavImages)
    $python = (Resolve-Path .venv/Scripts/python.exe).Path
    & $python -X utf8 tools/probes/windows/verify_product_selection.py $app.Id $hwnd.ToInt64() $folder.FullName
    if ($LASTEXITCODE -ne 0) { throw 'Product overlay verification failed' }
    $observations.selected = Wait-Text ImageStatus '64 × 48'
    Select-Element (Element NavTargets)
    Set-Value TargetName 'P1 captured'
    Invoke-Element SaveTarget
    $observations.target = Wait-Text Feedback '已保存目标 #'
    Select-Element (Element NavImages)
    Pick-Image $null
    $observations.picker_cancel = Wait-Text ImageStatus '已取消导入'
    Pick-Image $sample
    $observations.imported = Wait-Text ImageStatus '80 × 60'
    Select-Element (Element NavTargets)
    Invoke-Named '新建目标'
    Set-Value TargetName 'P1 imported'
    Invoke-Element SaveTarget
    $observations.target2 = Wait-Text Feedback '已保存目标 #2'
    Set-Value TargetName 'P1 renamed'
    Invoke-Element SaveTarget
    Wait-Text Feedback '已保存目标 #2' | Out-Null
    $expand = Find-Element '分组管理' -Name
    $expand.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    Set-Value GroupName 'P1 flow group'
    Invoke-Named '新建分组'
    $groupBox = Element TargetGroup
    $groupBox.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    $item = Wait-Until { $groupBox.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($ae::NameProperty,'P1 flow group'))) } 'created group option'
    Select-Element $item
    Invoke-Element SaveTarget
    Wait-Text Feedback '已保存目标 #2' | Out-Null
    $observations.group = 'P1 flow group'
    Invoke-Named '测试当前草稿'
    Invoke-Element TestRecognition
    $observations.recognition = Wait-Text Feedback '命中|未找到'
    Save-Tree 'recognition-result'
}
function Test-PersistedTarget {
    Select-Element (Element NavTargets)
    Wait-Until { Find-Element '#2　P1 renamed' -Name } 'renamed target after restart' | Out-Null
    Save-Tree 'reopened-targets'
    Select-Element (Element NavScripts)
    Set-Value ScriptSource 'wait(30)'
    Invoke-Element SaveScript
    Wait-Text ScriptFeedback '修订 2' | Out-Null
    Select-Element (Element NavRun)
    Invoke-Element ExecuteScript
    Wait-Text RunStatus '运行中' | Out-Null
    Invoke-Element PauseExecution
    $observations.ui_pause = Wait-Text RunStatus '已暂停'
    Invoke-Element PauseExecution
    Wait-Text RunStatus '运行中' | Out-Null
    Invoke-Element StopExecution
    $observations.ui_stop = Wait-Text RunStatus '已取消'
}
