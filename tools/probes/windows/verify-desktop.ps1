# Run in Windows PowerShell with an interactive desktop.
param(
    [Parameter(Mandatory)][string]$Executable,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [ValidateSet('normal','missing_models','crash_on_start','stdout_close_after_ready','stderr_flood','stdout_garbage','close_pending','exit_after_ready')]
    [string]$Scenario = 'normal'
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
$exe = (Resolve-Path -LiteralPath $Executable).Path
$folder = New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop
$env:SIDECAR_DATA_DIR = Join-Path $folder.FullName 'data'
if ($Scenario -in 'normal','missing_models','close_pending') { $env:SIDECAR_FAULT_SCENARIO = $null }
else { $env:SIDECAR_FAULT_SCENARIO = $Scenario }
if ($Scenario -eq 'missing_models') {
    $env:ST_OCR_MODELS_DIR = Join-Path $folder.FullName 'absent-models'
    & .venv/Scripts/python.exe -X utf8 (Join-Path $PSScriptRoot 'p2/seed.py') $env:SIDECAR_DATA_DIR
    if ($LASTEXITCODE) { throw 'Could not prepare the isolated script fixture' }
}
if ($Scenario -eq 'close_pending') { $env:ST_IPC_TRACE = '1' }
$observations = [ordered]@{scenario=$Scenario; started_at=[DateTimeOffset]::Now.ToString('o'); executable=$exe}

function Wait-For([scriptblock]$Check, [string]$Description, [int]$Seconds = 40) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        if ($app.HasExited) { throw "Desktop exited with $($app.ExitCode) while waiting for $Description" }
        $value = & $Check
        if ($value) { return $value }
        Start-Sleep -Milliseconds 120
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timeout waiting for $Description"
}

function Find-Control($Root, [string]$Id) {
    $condition = New-Object Windows.Automation.PropertyCondition(
        [Windows.Automation.AutomationElement]::AutomationIdProperty, $Id)
    $Root.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
}

function Invoke-Control($Root, [string]$Id) {
    $action = Wait-For {
        $control = Find-Control $Root $Id
        $pattern = $null
        if ($control -and $control.Current.IsEnabled -and
            $control.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) { $pattern }
    } $Id
    $action.Invoke()
}

function Close-NativeWindow($Window) {
    $action = Wait-For {
        $pattern = $null
        if ($Window.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern, [ref]$pattern)) { $pattern }
    } 'window close operation'
    $action.Close()
}

function Select-Page($Main,[string]$Id) {
    $item = Find-Control $Main $Id
    if (!$item -or $item.Current.IsOffscreen) {
        $more = Find-Control $Main 'TopNavOverflowButton'
        if ($more) { $more.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }
    }
    $item = Wait-For { Find-Control $Main $Id } $Id
    $pattern = $null
    if ($item.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) { $pattern.Select() }
    else { $item.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke() }
}

function Get-ChildrenOf([int]$Parent) {
    foreach ($child in (Get-CimInstance Win32_Process -Filter "ParentProcessId=$Parent")) {
        [int]$child.ProcessId
        Get-ChildrenOf ([int]$child.ProcessId)
    }
}

$startup = [Diagnostics.Stopwatch]::StartNew()
$app = Start-Process -FilePath $exe -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $folder 'stderr.log')
$observations.app_pid = $app.Id
$childIds = @()
try {
    $hwnd = Wait-For { $app.Refresh(); if ($app.MainWindowHandle.ToInt64() -ne 0) { $app.MainWindowHandle } } 'main HWND'
    $main = [Windows.Automation.AutomationElement]::FromHandle($hwnd)
    $health = Wait-For { Find-Control $main EngineHealth } 'health control'
    if ($Scenario -eq 'close_pending') {
        Wait-For { (Find-Control $main InspectModels).Current.IsEnabled } 'real OCR capability' | Out-Null
        Invoke-Control $main InspectModels
        Wait-For { Find-Control $main InstalledModels } 'main-window reply' | Out-Null
        $tracePath = Join-Path $folder.FullName 'stderr.log'
        $engine = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($app.Id)" |
            Where-Object { $_.CommandLine -match 'backend[\\/]main.py' -and $_.CommandLine.Contains($folder.FullName) })
        if ($engine.Count -ne 1) { throw 'Expected one owned Python sidecar in the isolated data directory' }
        $python = (Resolve-Path '.venv/Scripts/python.exe').Path
        $childIds = @(Get-ChildrenOf $app.Id)
        & $python -c 'import psutil,sys; psutil.Process(int(sys.argv[1])).suspend()' $engine[0].ProcessId
        if ($LASTEXITCODE) { throw 'Unable to suspend the isolated child' }
        try {
            Invoke-Control $main Diagnostics
            $diag = Wait-For {
                $match = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ProcessIdProperty,$app.Id)
                [Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Children,$match) |
                    Where-Object { $_.Current.Name -eq 'ScreenTrigger · 引擎诊断' } | Select-Object -First 1
            } 'diagnostic window'
            Wait-For { (Get-Content -Raw -Encoding utf8 -LiteralPath $tracePath) -match '\[desktop-ipc\] [^ ]+ ui-\d+ get_config' } 'secondary-window request started' | Out-Null
            $observations.in_flight_child = $engine[0].ProcessId
            $watch = [Diagnostics.Stopwatch]::StartNew()
            Close-NativeWindow $diag
            Invoke-Control $main StopEngine
            Wait-For { (Find-Control $main StartEngine).Current.IsEnabled } 'stop while secondary window has an in-flight request' 10 | Out-Null
            $observations.stop_ms = $watch.ElapsedMilliseconds
            if (Get-Process -Id $childIds -ErrorAction SilentlyContinue) { throw 'Stop left the blocked engine alive' }
            $lines = [regex]::Matches((Get-Content -Raw -Encoding utf8 -LiteralPath $tracePath),'\[desktop-ipc\] ([^ ]+) (ui-\d+) (hello|get_config)')
            if ($lines.Count -lt 2 -or (@($lines | ForEach-Object { $_.Groups[2].Value } | Select-Object -Unique)).Count -ne $lines.Count) { throw 'Cross-window request ID collision' }
            $observations.requests = @($lines | ForEach-Object { $_.Value })
        } finally {
            if ((Get-CimInstance Win32_Process -Filter "ProcessId=$($engine[0].ProcessId)" -ErrorAction SilentlyContinue).CreationDate -eq $engine[0].CreationDate) {
                & $python -c 'import psutil,sys; psutil.Process(int(sys.argv[1])).resume()' $engine[0].ProcessId
            }
        }
    } elseif ($Scenario -in 'crash_on_start','stdout_close_after_ready','exit_after_ready') {
        Wait-For { (Find-Control $main StartEngine).Current.IsEnabled -and $health.Current.Name -like '*退出*' } 'failure and restart availability' | Out-Null
        $observations.health_after_fault = $health.Current.Name
        if ($Scenario -eq 'stdout_close_after_ready') {
            $reason = Wait-For {
                $tree = $main.FindAll([Windows.Automation.TreeScope]::Descendants,
                    [Windows.Automation.Condition]::TrueCondition)
                $tree | ForEach-Object { $_.Current.Name } | Where-Object { $_ -match 'stdout_closed' } | Select-Object -First 1
            } 'stdout EOF reason (before startup watchdog)' 3
            $observations.fault_reason = $reason
        }
        $observations.after_fault_tree = @(Get-ChildrenOf $app.Id)
        if ($observations.after_fault_tree.Count) { throw 'Engine survived terminal fault' }
    } else {
        Wait-For { (Find-Control $main StopEngine).Current.IsEnabled } 'ready' | Out-Null
        $observations.health = $health.Current.Name
        $observations.ready_ms = $startup.Elapsed.TotalMilliseconds
        $row = Wait-For { Find-Control $main Capability_ocr } 'OCR capability'
        $observations.ocr = $row.Current.Name
        $modelButton = Wait-For { Find-Control $main InspectModels } 'model inventory button'
        $observations.model_button_enabled = $modelButton.Current.IsEnabled
        if ($observations.ocr -match 'OCR.*不可用') {
            if ($modelButton.Current.IsEnabled) { throw 'Unavailable OCR exposed an enabled model action' }
            if ($Scenario -eq 'missing_models') {
                Select-Page $main NavRun
                Invoke-Control $main ExecuteScript
                $observations.pure_script = Wait-For { $s=Find-Control $main RunStatus; if ($s.Current.Name -match '已完成') { $s.Current.Name } } 'Lua without OCR'
                Select-Page $main NavEngine
            }
        } else {
            if ($Scenario -eq 'missing_models' -or !$modelButton.Current.IsEnabled) { throw "Inconsistent OCR state: $($observations.ocr)" }
            Invoke-Control $main InspectModels
            $inventory = Wait-For { Find-Control $main InstalledModels } 'installed model IDs'
            $observations.models = $inventory.Current.Name
            if ($observations.models -notmatch '\S' -or $observations.models -match '未返回') { throw 'Ready OCR returned no installed model IDs' }
            Invoke-Control $main InspectModels
        }
        $childIds = @(Get-ChildrenOf $app.Id)
        if ($childIds.Count -eq 0) { throw 'Missing engine process' }
        $observations.first_tree = $childIds
        $observations.processes = @(Get-Process -Id (@($app.Id)+$childIds) | ForEach-Object {
            [ordered]@{pid=$_.Id; name=$_.ProcessName; image=$_.Path; rss=$_.WorkingSet64; private_bytes=$_.PrivateMemorySize64}
        })
        $observations.modules = @((Get-Process -Id $app.Id).Modules | ForEach-Object ModuleName)
        $install = Split-Path $exe -Parent
        if (Test-Path -LiteralPath (Join-Path $install 'package.json')) {
            $loaded = @((Get-Process -Id $app.Id).Modules)
            $runtimePaths = [ordered]@{}
            foreach ($name in 'coreclr.dll','Microsoft.UI.Xaml.dll','screen_trigger_lib.dll') {
                $path = @($loaded | Where-Object ModuleName -eq $name | ForEach-Object FileName)
                if ($path.Count -ne 1 -or $path[0] -ne (Join-Path $install $name)) {
                    throw "Unexpected loaded runtime path for $name"
                }
                $runtimePaths[$name] = $path[0]
            }
            $engines = @(Get-Process -Id $childIds | Where-Object Path -eq (Join-Path $install 'resources/python/python.exe'))
            if (!$engines.Count) {throw 'Packaged interpreter missing from the running process tree'}
            foreach ($engine in $engines) {
                $dll = @($engine.Modules | Where-Object ModuleName -eq 'python313.dll' | ForEach-Object FileName)
                if ($dll.Count -ne 1 -or $dll[0] -ne (Join-Path $install 'resources/python/python313.dll')) {
                    throw 'Unexpected loaded CPython library path'
                }
                $runtimePaths["python-$($engine.Id)"] = @($engine.Path,$dll[0])
            }
            $observations.runtimePaths = $runtimePaths
        }
        if ($observations.processes.name -match 'msedgewebview2') { throw 'Unexpected WebView2 process' }
        foreach ($iteration in 1,2,3) {
            if ($iteration -eq 3) {
                Invoke-Control $main StopEngine
                Wait-For { (Find-Control $main StartEngine).Current.IsEnabled } 'stop' | Out-Null
                if (Get-Process -Id $childIds -ErrorAction SilentlyContinue) { throw 'Engine survived Stop' }
                Invoke-Control $main StartEngine
                Wait-For { (Find-Control $main StopEngine).Current.IsEnabled } 'restart' | Out-Null
                $childIds = @(Get-ChildrenOf $app.Id)
                if (!$childIds.Count) { throw 'Engine missing after restart' }
                $observations.restarted_tree = $childIds
            }
            Invoke-Control $main Diagnostics
            $diag = Wait-For {
                $pidMatch = New-Object Windows.Automation.PropertyCondition(
                    [Windows.Automation.AutomationElement]::ProcessIdProperty, $app.Id)
                $windows = [Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $pidMatch)
                foreach ($window in $windows) {
                    if ($window.Current.Name -eq 'ScreenTrigger · 引擎诊断' -and (Find-Control $window EngineDiagnostics)) { return $window }
                }
            } 'native diagnostics window'
            $snapshot = (Find-Control $diag EngineDiagnostics).Current.Name
            if ($snapshot -notmatch 'seq [1-9]' -or $snapshot -notmatch '(?i)true' -or $snapshot -notmatch '配置修订\s+[1-9]') {
                throw "Missing ready event, configuration response or job object. $snapshot"
            }
            $observations["diagnostics_$iteration"] = $snapshot
            if ($Scenario -eq 'stdout_garbage' -and $snapshot -notmatch '协议异常行\s+4(?:\s|$)') {
                throw "Expected four discarded frames and a live session. $snapshot"
            }
            if ($iteration -eq 3 -and ($snapshot -split "\n")[0] -eq ($observations.diagnostics_1 -split "\n")[0]) {
                throw 'Restart reused the previous session'
            }
            Close-NativeWindow $diag
            Wait-For {
                $remaining = [Windows.Automation.AutomationElement]::RootElement.FindAll(
                    [Windows.Automation.TreeScope]::Children, [Windows.Automation.Condition]::TrueCondition)
                return !($remaining | Where-Object { $_.Current.ProcessId -eq $app.Id -and $_.Current.Name -eq 'ScreenTrigger · 引擎诊断' })
            } 'diagnostics closed' | Out-Null
        }
    }
    Close-NativeWindow $main
    if (!$app.WaitForExit(10000)) {
        $observations.remaining_tree = @(Get-ChildrenOf $app.Id)
        $windows = [Windows.Automation.AutomationElement]::RootElement.FindAll(
            [Windows.Automation.TreeScope]::Children, [Windows.Automation.Condition]::TrueCondition)
        $observations.remaining_windows = @($windows | Where-Object { $_.Current.ProcessId -eq $app.Id } | ForEach-Object { $_.Current.Name })
        throw 'Desktop did not exit'
    }
    $observations.exit_code = $app.ExitCode
    $observations.remaining = @()
    if ($childIds.Count) {
        $observations.remaining = @(Get-Process -Id $childIds -ErrorAction SilentlyContinue | ForEach-Object Id)
    }
    if ($app.ExitCode -ne 0 -or $observations.remaining.Count) { throw 'Unclean desktop exit' }
    $observations.result = 'PASS'
} catch {
    $observations.result = 'FAIL'
    $observations.error = $_.Exception.Message
    $observations.failed_step = $_.ScriptStackTrace
} finally {
    if (!$app.HasExited) {
        [void]$app.CloseMainWindow()
        if (!$app.WaitForExit(5000)) { $app.Kill() }
    }
    $observations.finished_at = [DateTimeOffset]::Now.ToString('o')
    $observations | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $folder 'result.json') -Encoding UTF8
}
$observations | ConvertTo-Json -Depth 5
if ($observations.result -ne 'PASS') { exit 1 }
