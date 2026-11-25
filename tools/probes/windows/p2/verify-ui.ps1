param([Parameter(Mandatory)][string]$OutputDirectory, [switch]$DebugCases, [switch]$HistoryCases,
      [switch]$BackupCases, [switch]$InteractionCases, [switch]$KeyboardCases,
      [string]$Executable = 'dist/desktop/bin/ScreenTrigger/debug_win-x64/ScreenTrigger.exe')
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$folder=New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop
$env:SIDECAR_DATA_DIR=Join-Path $folder.FullName 'data'
$env:SIDECAR_FAULT_SCENARIO=$null
& .venv/Scripts/python.exe -X utf8 (Join-Path $PSScriptRoot 'seed.py') $env:SIDECAR_DATA_DIR
if ($LASTEXITCODE -ne 0) { throw 'isolated fixture failed' }
$ae=[Windows.Automation.AutomationElement]
$tree=[Windows.Automation.TreeScope]::Descendants
$seen=[ordered]@{}
function Wait-For([scriptblock]$read,[string]$what) {
    $end=[DateTime]::UtcNow.AddSeconds(35)
    do {
        if ($app.HasExited) { throw "Application exited before $what" }
        try { $item=& $read; if ($item) { return $item } } catch [System.Windows.Automation.ElementNotAvailableException] {}
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $end)
    throw "Timed out waiting for $what"
}
function Find-Item([string]$key, [switch]$Name) {
    $property=if ($Name) {$ae::NameProperty} else {$ae::AutomationIdProperty}
    $main.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($property,$key)))
}
function Item([string]$key) { Wait-For { $e=Find-Item $key; if ($e -and $e.Current.IsEnabled) { $e } } $key }
function Invoke-ItemButton([string]$key) { (Item $key).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }
function Select-Item($element) {
    $pattern=$null
    if ($element.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) {$pattern.Select()}
    else {$element.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()}
}
function Navigate([string]$key) {
    $nav=Find-Item $key
    if (!$nav -or $nav.Current.IsOffscreen) {
        $more=Find-Item 'TopNavOverflowButton'
        if ($more) {$more.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()}
    }
    Select-Item (Item $key)
}
function Save-Tree([string]$label) {
    @($main.FindAll($tree,[Windows.Automation.Condition]::TrueCondition) | ForEach-Object {
        [ordered]@{name=$_.Current.Name; id=$_.Current.AutomationId; type=$_.Current.ControlType.ProgrammaticName; keyboard=$_.Current.IsKeyboardFocusable}
    }) | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath (Join-Path $folder.FullName "$label.json")
}
function Set-Field([string]$key,[string]$value) {
    $e=Item $key
    $v=$null
    if ($e.TryGetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern,[ref]$v)) { $v.SetValue([double]$value); return }
    if (!$e.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$v)) {
        $e=$e.FindFirst($tree,(New-Object Windows.Automation.PropertyCondition($ae::ControlTypeProperty,[Windows.Automation.ControlType]::Edit)))
        $v=$e.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern)
    }
    $v.SetValue($value)
}
try {
    $exe=(Resolve-Path -LiteralPath $Executable).Path
    $app=Start-Process -FilePath $exe -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $folder.FullName 'stderr.log')
    $handle=Wait-For { $app.Refresh(); if ($app.MainWindowHandle -ne [IntPtr]::Zero) {$app.MainWindowHandle} } 'main'
    $main=$ae::FromHandle($handle)
    Item StopEngine | Out-Null
    Navigate NavRun
    Item ExecuteScript | Out-Null
    Save-Tree 'run'
    Navigate NavMonitor
    Item StartMonitor | Out-Null
    Save-Tree 'monitor'
    Set-Field WaitTimeout '400'
    Invoke-ItemButton StartMonitor
    $seen.terminal=Wait-For { $s=Find-Item MonitorObservation; if ($s.Current.Name -match 'WAIT_TIMEOUT') {$s.Current.Name} } 'wait timeout'
    Save-Tree 'timeout'
    if ((Find-Item DebugVariables) -or (Find-Item HistoryLogs)) {throw 'ordinary monitor opened a debug or history panel'}
    if ($DebugCases) { . (Join-Path $PSScriptRoot 'debug-ui.ps1') }
    if ($HistoryCases) { . (Join-Path $PSScriptRoot 'history-ui.ps1') }
    if ($BackupCases) { . (Join-Path $PSScriptRoot '../../../../tests/acceptance/backup-ui.ps1') }
    if ($InteractionCases) { . (Join-Path $PSScriptRoot '../../../../tests/acceptance/native_interactions.ps1') }
    if ($KeyboardCases) { . (Join-Path $PSScriptRoot '../../../../tests/acceptance/keyboard_pages.ps1') }
    $main.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (!$app.WaitForExit(15000) -or $app.ExitCode -ne 0) { throw 'unclean shutdown' }
} catch {
    if ($main -and !$app.HasExited) { Save-Tree 'failure' }
    throw
} finally {
    if ($app -and !$app.HasExited) {
        $app.CloseMainWindow() | Out-Null
        if (!$app.WaitForExit(5000)) { $app.Kill() }
    }
    $seen | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $folder.FullName 'observations.json') -Encoding utf8
}
