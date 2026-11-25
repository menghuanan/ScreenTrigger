param([Parameter(Mandatory)][string]$Executable, [Parameter(Mandatory)][string]$OutputDirectory, [switch]$Extended)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$folder = New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop
$env:SIDECAR_DATA_DIR = Join-Path $folder.FullName 'data'
$env:SIDECAR_FAULT_SCENARIO = $null
$exe = (Resolve-Path -LiteralPath $Executable).Path
$tree = [Windows.Automation.TreeScope]::Descendants
$ae = [Windows.Automation.AutomationElement]
$observations = [ordered]@{}
function Wait-Until([scriptblock]$Read, [string]$What) {
    $deadline = [DateTime]::UtcNow.AddSeconds(35)
    do {
        if ($app.HasExited) { throw "Application exited while waiting for $What" }
        try { $value = & $Read; if ($value) { return $value } }
        catch [System.Windows.Automation.ElementNotAvailableException] {}
        Start-Sleep -Milliseconds 120
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for $What"
}
function Find-Element($key, [switch]$Name) {
    $property = if ($Name) { $ae::NameProperty } else { $ae::AutomationIdProperty }
    $condition = New-Object Windows.Automation.PropertyCondition($property, $key)
    return $main.FindFirst($tree, $condition)
}
function Element($key) { Wait-Until { $c = Find-Element $key; if ($c.Current.IsEnabled) { $c } } $key }
function Invoke-Element($key) {
    (Element $key).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Select-Element($item) {
    $pattern = $null
    if ($item.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) { $pattern.Select() }
    else { $item.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }
}
function Set-Value($key, $value) { (Element $key).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).SetValue($value) }
function Save-Tree($tag) {
    $rows = @($main.FindAll($tree, [Windows.Automation.Condition]::TrueCondition) | ForEach-Object {
        [ordered]@{ type=$_.Current.ControlType.ProgrammaticName; id=$_.Current.AutomationId; name=$_.Current.Name }
    })
    $rows | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $folder.FullName "$tag.json")
}
if ($Extended) { . (Join-Path $PSScriptRoot 'workspace-image-cases.ps1') }
$source = 'return {sample = 21}'
$label = 'P1 workspace smoke'
try {
    foreach ($iteration in 1..2) {
        $app = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $folder.FullName "stderr-$iteration.log")
        $hwnd = Wait-Until { $app.Refresh(); if ($app.MainWindowHandle -ne [IntPtr]::Zero) { $app.MainWindowHandle } } 'WinUI window'
        $main = $ae::FromHandle($hwnd)
        Element StopEngine | Out-Null
        foreach ($entry in @(@('NavImages','StartSelection'),@('NavTargets','TargetName'),@('NavRecognition','TestRecognition'),@('NavScripts','ScriptSource'))) {
            Select-Element (Element $entry[0])
            Wait-Until { Find-Element $entry[1] } $entry[1] | Out-Null
            Save-Tree "$iteration-$($entry[0])"
        }
        if ($iteration -eq 1) {
            Set-Value ScriptName $label
            Set-Value ScriptSource 'return ('
            Invoke-Element ValidateScript
            Wait-Until { $feedback = Find-Element ScriptFeedback; if ($feedback.Current.Name -match '第.*行') { $feedback.Current.Name } } 'syntax diagnostic' |
                ForEach-Object { $observations.syntax = $_ }
            Set-Value ScriptSource $source
            Invoke-Element SaveScript
            Wait-Until { $feedback = Find-Element ScriptFeedback; if ($feedback.Current.Name -match '修订 1') { $feedback.Current.Name } } 'saved revision' |
                ForEach-Object { $observations.saved = $_ }
            if ($Extended) { Test-ImageWorkspace }
        } else {
            $row = Wait-Until { Find-Element $label -Name } 'previously saved script'
            Select-Element $row
            (Find-Element '打开脚本' -Name).GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
            Wait-Until { (Element ScriptSource).GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).Current.Value -eq $source } 'persisted Lua source' | Out-Null
            $observations.reopened_source = $source
            if ($Extended) { Test-PersistedTarget }
        }
        $main.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
        if (!$app.WaitForExit(15000)) { throw 'Application did not exit' }
    }
    if ($Extended) {
        & .venv/Scripts/python.exe -X utf8 tools/probes/windows/verify_product_selection.py --verify-assets $folder.FullName
        if ($LASTEXITCODE -ne 0) { throw 'Persisted image or catalog mismatch' }
    }
} catch {
    Write-Output $_.ScriptStackTrace
    throw
} finally {
    if ($app -and !$app.HasExited) {
        try { Save-Tree 'failure'; $app.CloseMainWindow() | Out-Null } catch {}
        if (!$app.WaitForExit(5000)) { $app.Kill() }
    }
    $observations | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $folder.FullName 'observations.json')
}
