# Runs inside verify-ui.ps1, which owns the isolated WinUI window and data root.
Add-Type -AssemblyName System.Windows.Forms
function Press-BackupKey($control,[string]$key) {
    $control.SetFocus()
    $focused=Wait-For { if ($control.Current.HasKeyboardFocus) { $ae::FocusedElement } } 'keyboard focus inside the backup control'
    if ($focused.Current.ProcessId -ne $app.Id) { throw 'Backup keyboard target is outside the test application' }
    [System.Windows.Forms.SendKeys]::SendWait($key)
}
function Read-RestoredFocus {
    Wait-For { $e=$ae::FocusedElement; if ($e.Current.ProcessId -eq $app.Id -and $e.Current.AutomationId -eq 'PreviewRestore') { $e.Current.AutomationId } } 'restore preview button regains focus'
}
Navigate NavSettings
(Item CatalogBackups).GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
Invoke-ItemButton PreviewRestore
$seen.emptyBackupMessage=Wait-For { $e=Find-Item SettingsFeedback; if ($e.Current.Name -match '请先选择备份') {$e.Current.Name} } 'select a backup before restoring'
Press-BackupKey (Item CreateBackup) '{ENTER}'
$seen.catalogBackup = Wait-For { $s=Find-Item SettingsFeedback; if ($s.Current.Name -match '备份已保存') { $s.Current.Name } } 'catalog backup complete'
$current = Join-Path $env:SIDECAR_DATA_DIR 'catalog/current.json'
$before = (Get-FileHash -LiteralPath $current).Hash
Press-BackupKey (Item PreviewRestore) '{ENTER}'
$null = Wait-For { Find-Item '恢复这些目标与脚本？' -Name } 'restore preview'
Save-Tree 'restore-preview'
Press-BackupKey (Find-Item '取消' -Name) '{ESC}'
$null = Wait-For { (Find-Item SettingsFeedback).Current.Name -match '已取消恢复' } 'cancel restore'
if ((Get-FileHash -LiteralPath $current).Hash -ne $before) { throw 'Cancel changed current' }
$seen.cancelFocus=Read-RestoredFocus
Press-BackupKey (Item PreviewRestore) '{ENTER}'
$null = Wait-For { Find-Item '恢复这些目标与脚本？' -Name } 'confirmable preview'
Press-BackupKey (Find-Item '确认恢复' -Name) '{ENTER}'
$seen.catalogRestore = Wait-For { $s=Find-Item SettingsFeedback; if ($s.Current.Name -match '已恢复，当前 generation') { $s.Current.Name } } 'restore commit'
if ((Get-FileHash -LiteralPath $current).Hash -eq $before) { throw 'Restored pointer did not switch' }
$seen.backupCount = @(Get-ChildItem -LiteralPath (Join-Path $env:SIDECAR_DATA_DIR 'backups') -Directory -Filter 'b-*').Count
if ($seen.backupCount -lt 2) { throw 'Missing protection backup' }
$seen.confirmFocus=Read-RestoredFocus
Save-Tree 'restored'
