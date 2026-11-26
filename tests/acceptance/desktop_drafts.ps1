# Sourced by verify-workspace.ps1. Every image and edit belongs to its new data root.
Add-Type -AssemblyName System.Windows.Forms
function Read-Value($key) {
    (Element $key).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value
}
function Set-Number($key, [double]$value) {
    $control=Element $key
    $control.GetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern).SetValue($value)
}
function Leave-Draft($name, $choice) {
    $null=Wait-Until {Find-Element "保存${name}的修改？" -Name} "$name draft dialog"
    Save-Tree "draft-$name-$choice"
    $button=Wait-Until {Find-Element $choice -Name} "$choice button"
    $button.SetFocus()
    $null=Wait-Until {$button.Current.HasKeyboardFocus -and $ae::FocusedElement.Current.ProcessId -eq $app.Id} 'choice in owned dialog'
    [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
}
function Request-Close {
    $main.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
}
function Open-SettingsGroup($key) {
    $group=Element "SettingGroup_$key"
    $expand=$group.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
    if ($expand.Current.ExpandCollapseState -eq [Windows.Automation.ExpandCollapseState]::Collapsed) { $expand.Expand() }
}
function Set-PreviewRate([int]$hz) {
    Open-SettingsGroup capture
    Set-Number Setting_capture_preview_hz $hz
}
function Test-DesktopDrafts($iteration) {
    $sample=Join-Path $folder.FullName 'source.png'
    $packaged=Test-Path -LiteralPath (Join-Path (Split-Path -Parent $exe) 'resources/weights')
    if ($iteration -eq 2) {
        if ($packaged) {
            $observations.customModelState=Wait-Text Capability_ocr '不可用'
            Save-Tree 'custom-model-directory'
            Select-Element (Element NavSettings)
            Open-SettingsGroup recognition
            $observations.reopenedModelDirectory=Read-Value Setting_recognition_model_directory
            $observations.reopenedOcrThreshold=(Element Setting_recognition_ocr_min_confidence).GetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern).Current.Value
            Open-SettingsGroup capture
            $observations.reopenedPngLevel=(Element Setting_capture_png_compression_level).GetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern).Current.Value
            $saved=(Get-Content -Raw -LiteralPath (Join-Path $env:SIDECAR_DATA_DIR 'config.json') | ConvertFrom-Json)
            if ($observations.reopenedModelDirectory -ne 'qa-missing-models' -or
                [Math]::Abs($observations.reopenedOcrThreshold - 0.95) -gt 0.000001 -or
                $saved.recognition.ocr_min_confidence -ne 0.95 -or $observations.reopenedPngLevel -ne 9) { throw 'Reopened settings mismatch' }
            Set-Value Setting_recognition_model_directory 'models'
            Invoke-Element SaveSettings
            $null=Wait-Text SettingsFeedback '设置已保存'
            Select-Element (Element NavEngine)
            Invoke-Element StopEngine
            $null=Element StartEngine
            Invoke-Element StartEngine
            $null=Element StopEngine
            Invoke-Element InspectModels
            $observations.restoredModels=Wait-Text InstalledModels 'PP-OCR'
            Save-Tree 'default-model-directory'
        }
        Select-Element (Element NavScripts)
        Select-Element (Wait-Until {Find-Element '草稿测试' -Name} 'saved script after restart')
        Invoke-Named '打开脚本'
        $null=Wait-Until {(Read-Value ScriptSource) -eq 'return 17'} 'persisted script'
        Set-Value ScriptSource 'return 88'
        $observations.beforeDiscard=(Get-FileHash -LiteralPath (Join-Path $env:SIDECAR_DATA_DIR 'catalog/current.json')).Hash
        Request-Close
        Leave-Draft '脚本' '放弃'
        $observations.reopenedSource='return 17'
        return
    }
    Select-Element (Element NavImages)
    $bitmap=[Drawing.Bitmap]::new(80,60)
    try {
        for ($y=0; $y -lt 60; $y++) {
            for ($x=0; $x -lt 80; $x++) { $bitmap.SetPixel($x,$y,[Drawing.Color]::FromArgb($x*3,$y*4,($x+$y)%256)) }
        }
        $bitmap.Save($sample,[Drawing.Imaging.ImageFormat]::Png)
    } finally { $bitmap.Dispose() }
    $originalHash=(Get-FileHash -LiteralPath $sample).Hash
    Pick-Image $null
    $null=Wait-Text ImageStatus '已取消导入'
    $observations.pickerCancelFocus=Wait-Until {if ((Element ImportImage).Current.HasKeyboardFocus) {'ImportImage'}} 'picker cancel returns keyboard focus'
    Pick-Image $sample
    $null=Wait-Text ImageStatus '80 × 60'
    Invoke-Element CropImage
    Set-Number CropWidth 81
    $null=Wait-Until { $button=Find-Element '使用裁剪图' -Name; $button -and !$button.Current.IsEnabled } 'out of bounds disabled'
    Set-Number CropWidth 20
    Set-Number CropHeight 16
    Set-Number CropX 11
    Set-Number CropY 7
    Invoke-Named '使用裁剪图'
    $observations.cropped=Wait-Text ImageStatus '20 × 16'
    Select-Element (Element NavTargets)
    Set-Value TargetName '裁剪样本'
    Invoke-Element SaveTarget
    $null=Wait-Text Feedback '已保存目标 #1'
    $catalog=Join-Path $env:SIDECAR_DATA_DIR 'catalog'
    $generation=(Get-Content -Raw -LiteralPath (Join-Path $catalog 'current.json')|ConvertFrom-Json).generation
    $target=(Get-Content -Encoding utf8 -Raw -LiteralPath (Join-Path $catalog "generations/$generation/targets.json")|ConvertFrom-Json).targets[0]
    $asset=Join-Path $env:SIDECAR_DATA_DIR $target.image.path
    $actual=[Drawing.Bitmap]::new($asset)
    $original=[Drawing.Bitmap]::new($sample)
    try {
        if ($actual.Width -ne 20 -or $actual.Height -ne 16) {throw 'Saved crop dimensions changed'}
        for ($y=0; $y -lt 16; $y++) {
            for ($x=0; $x -lt 20; $x++) {
                if ($actual.GetPixel($x,$y).ToArgb() -ne $original.GetPixel(11+$x,7+$y).ToArgb()) {throw "Wrong saved pixel $x,$y"}
            }
        }
    } finally { $actual.Dispose(); $original.Dispose() }
    $observations.crop=@{comparedPixels=320; source=$originalHash; asset=(Get-FileHash -LiteralPath $asset).Hash; generation=$generation}
    Select-Element (Element NavImages)
    Invoke-Element RestoreOriginal
    $null=Wait-Text ImageStatus '80 × 60'
    if ((Get-FileHash -LiteralPath $sample).Hash -ne $originalHash) {throw 'Source changed'}
    & .venv/Scripts/python.exe -X utf8 tests/acceptance/overlay_boundaries.py $app.Id $hwnd.ToInt64() $folder.FullName
    if ($LASTEXITCODE -ne 0) { throw 'Real overlay focus, boundary or capture fault failed' }
    Select-Element (Element NavSettings)
    foreach ($hz in @(1,20)) {
        Set-PreviewRate $hz
        Invoke-Element SaveSettings
        $null=Wait-Text SettingsFeedback '设置已保存'
        Select-Element (Element NavImages)
        & .venv/Scripts/python.exe -X utf8 tools/probes/windows/verify_product_selection.py $app.Id $hwnd.ToInt64() $folder.FullName $hz
        if ($LASTEXITCODE -ne 0) {throw "Actual preview cadence failed at $hz Hz"}
        Select-Element (Element NavSettings)
    }
    Set-PreviewRate 3
    & .venv/Scripts/python.exe -c 'import sys; from backend.config.service import ConfigService; svc=ConfigService(sys.argv[1]); doc=svc.get_config(); svc.update_config({"capture":{"preview_hz":7}},doc["revision"])' $env:SIDECAR_DATA_DIR
    if ($LASTEXITCODE -ne 0) { throw 'Concurrent settings write failed' }
    Request-Close
    Leave-Draft '设置' '保存'
    $observations.conflict=Wait-Text SettingsFeedback 'CONFLICT'
    if ((Element Setting_capture_preview_hz).GetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern).Current.Value -ne 3) { throw 'Conflict discarded unsaved settings' }
    Invoke-Named '重新读取'
    Leave-Draft '设置' '放弃'
    $null=Element SaveSettings
    if ($packaged) {
        $null=New-Item -ItemType Directory -Path (Join-Path $env:SIDECAR_DATA_DIR 'qa-missing-models')
        Open-SettingsGroup recognition
        Set-Value Setting_recognition_model_directory 'qa-missing-models'
        $ocrBox=Element Setting_recognition_ocr_min_confidence
        $inputBox=$ocrBox.FindFirst($tree,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,'InputBox'))
        $inputBox.SetFocus()
        $null=Wait-Until {$ae::FocusedElement.Current.ProcessId -eq $app.Id -and $inputBox.Current.HasKeyboardFocus} 'OCR confidence input focus'
        [System.Windows.Forms.SendKeys]::SendWait('^a0.95{ENTER}')
        Open-SettingsGroup capture
        Set-Number Setting_capture_png_compression_level 9
        Invoke-Element SaveSettings
        $null=Wait-Text SettingsFeedback '设置已保存'
    }
    Select-Element (Element NavScripts)
    Set-Value ScriptName '草稿测试'
    Set-Value ScriptSource 'return 17'
    (Element ScriptSource).SetFocus()
    Request-Close
    Leave-Draft '脚本' '取消'
    if ((Read-Value ScriptSource) -ne 'return 17') { throw 'Cancel lost script draft' }
    $observations.cancelFocus=Wait-Until {if ((Element ScriptSource).Current.HasKeyboardFocus) {'ScriptSource'}} 'close cancellation restores source focus'
    Request-Close
    Leave-Draft '脚本' '保存'
}
