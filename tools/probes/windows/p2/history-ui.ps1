Navigate NavHistory
Invoke-ItemButton RefreshHistory
$seen.history=Wait-For { $e=Find-Item HistoryLogs; if ($e.Current.Name -match 'completed|failed') { $e.Current.Name } } 'persisted execution logs'
Save-Tree 'history'
(Item RequestLookup).GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Set-Field RequestId 'p2-unknown-id'
(Item RequestId).SetFocus()
Add-Type -AssemblyName System.Windows.Forms
$focusBefore=$ae::FocusedElement.Current
if ($focusBefore.ProcessId -ne $app.Id) { throw 'Keyboard focus outside the fixture' }
[System.Windows.Forms.SendKeys]::SendWait('{TAB}')
$seen.queryTab=Wait-For {
    $focused=$ae::FocusedElement.Current
    if ($focused.ProcessId -eq $app.Id -and $focused.Name -eq '查询状态') { $focused.Name }
} 'Tab from request ID to query button'
(Find-Item '查询状态' -Name).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
$seen.unknown=Wait-For { $e=Find-Item HistoryFeedback; if ($e.Current.Name -match '未知') {$e.Current.Name} } 'unknown request'
Navigate NavSettings
Item Setting_general_theme | Out-Null
$previewKey='Setting_capture_preview_hz'
$group=Item SettingGroup_capture
$group.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Set-Field $previewKey '12'
Invoke-ItemButton SaveSettings
$seen.save=Wait-For { $e=Find-Item SettingsFeedback; if ($e.Current.Name -match '设置已保存') {$e.Current.Name} } 'saved settings'
$configPath=Join-Path $env:SIDECAR_DATA_DIR 'config.json'
$hash=(Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
$group=Item SettingGroup_capture
$group.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Set-Field $previewKey '14'
Invoke-ItemButton CancelSettings
Wait-For { (Find-Item SettingsFeedback).Current.Name -match '已撤销' } 'cancel change' | Out-Null
if ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $hash) { throw 'Cancel wrote config' }
Invoke-ItemButton ResetSettings
$dialog=Wait-For { Find-Item '恢复默认设置？' -Name } 'preview reset differences'
Save-Tree 'reset-preview'
(Find-Item '取消' -Name).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
if ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $hash) { throw 'Cancel reset wrote config' }
Save-Tree 'settings'
Navigate NavEngine
Invoke-ItemButton StopEngine
Item StartEngine | Out-Null
$dataPath=(Resolve-Path -LiteralPath $env:SIDECAR_DATA_DIR).Path
if ($dataPath -ne (Join-Path $folder.FullName 'data')) { throw 'unexpected fixture directory' }
$backup=Get-FileHash -LiteralPath (Join-Path $dataPath 'backups/config.json.bak') -Algorithm SHA256
$histories=@(Get-ChildItem -LiteralPath (Join-Path $dataPath 'history') -Filter '*.json' | Select-Object -ExpandProperty Name)
Set-Content -LiteralPath $configPath -Value '{broken P2 fixture' -Encoding utf8NoBOM
Invoke-ItemButton StartEngine
Item StopEngine | Out-Null
Navigate NavSettings
$seen.recovery=Wait-For { $e=Find-Item ConfigRecovery; if ($e.Current.Name -match '自动恢复') {$e.Current.Name} } 'automatic recovery disclosed'
if ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne $backup.Hash) {throw 'backup not restored byte for byte'}
Navigate NavHistory
Invoke-ItemButton RefreshHistory
Wait-For { (Find-Item HistoryLogs).Current.Name -match 'completed|failed' } 'reopened history' | Out-Null
$after=@(Get-ChildItem -LiteralPath (Join-Path $dataPath 'history') -Filter '*.json' | Select-Object -ExpandProperty Name)
if (Compare-Object $histories $after) { throw 'restart created additional executions' }
Save-Tree 'reopened-history'
Invoke-ItemButton Clear
(Wait-For { Find-Item '取消' -Name } 'cancel clear').GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
Wait-For { (Find-Item HistoryLogs).Current.Name -match 'completed|failed' } 'logs retained' | Out-Null
Invoke-ItemButton Clear
(Wait-For { Find-Item '清除日志' -Name } 'confirm clear').GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
$seen.cleared=Wait-For { $e=Find-Item HistoryFeedback; if ($e.Current.Name -match '已清除') { $e.Current.Name } } 'logs cleared'
if (Compare-Object $histories @(Get-ChildItem -LiteralPath (Join-Path $dataPath 'history') -Filter '*.json' | Select-Object -ExpandProperty Name)) { throw 'clearing logs removed an execution summary' }
$seen.config_cancel_sha256=$hash
$seen.restored_sha256=$backup.Hash
$seen.history_after_restart=$after

foreach ($entry in @(@('NavImages','StartSelection'),@('NavTargets','TargetName'),@('NavRecognition','TestRecognition'),@('NavEngine','Diagnostics'))) {
    Navigate $entry[0]
    Wait-For { Find-Item $entry[1] } $entry[1] | Out-Null
    Save-Tree $entry[0]
}
Invoke-ItemButton Diagnostics
$diag=Wait-For {
    $own=New-Object Windows.Automation.PropertyCondition($ae::ProcessIdProperty,$app.Id)
    $windows=$ae::RootElement.FindAll([Windows.Automation.TreeScope]::Children,$own)
    $windows | Where-Object { $_.Current.Name -eq 'ScreenTrigger · 引擎诊断' } | Select-Object -First 1
} 'diagnostics window in same process'
$seen.diagnostics=$diag.Current.Name
$diag.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
Navigate NavImages
& .venv/Scripts/python.exe -X utf8 tools/probes/windows/verify_product_selection.py $app.Id $handle.ToInt64() $folder.FullName
if ($LASTEXITCODE -ne 0) { throw 'Overlay did not return to the original window' }
$seen.overlay=Wait-For { $e=Find-Item ImageStatus; if ($e.Current.Name -match '64 × 48') { $e.Current.Name } } 'selected physical region'
