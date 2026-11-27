function Measure-StartupUi($process, $record, [string]$manifest) {
    $scenario = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $scope = [Windows.Automation.TreeScope]::Descendants
    $ae = [Windows.Automation.AutomationElement]
    $now = { [Diagnostics.Stopwatch]::GetTimestamp() }
    function Find-StartupControl($root, [string]$id) {
        $condition = [Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,$id)
        return $root.FindFirst($scope,$condition)
    }
    function Await-Startup($get, [string]$label, [int]$seconds=100) {
        $timer = [Diagnostics.Stopwatch]::StartNew()
        do {
            if ($process.HasExited) { throw "Application exited while waiting for $label" }
            try { if ($value = & $get) { return $value } }
            catch [Windows.Automation.ElementNotAvailableException] { }
            if ($timer.Elapsed.TotalSeconds -ge $seconds) { throw "Timeout waiting for $label" }
            Start-Sleep -Milliseconds 20
        } while ($true)
    }
    $record.process_observed_qpc = & $now
    $record.process_started_utc = $process.StartTime.ToUniversalTime().ToString('o')
    $record.runs = @()
    $window = Await-Startup { $process.Refresh(); if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
        $ae::FromHandle($process.MainWindowHandle) } } 'WinUI window'
    $record.window_qpc = & $now
    $nav = Await-Startup {
        $legacy = Find-StartupControl $window 'NavRun'
        if ($legacy) { $legacy }
        elseif ($work = Find-StartupControl $window 'NavWork') { $work }
        else { Find-StartupControl $window 'NavScripts' }
    } 'script navigation'
    $workPage = $nav.Current.AutomationId -eq 'NavWork'
    $userClient = $workPage -or $nav.Current.AutomationId -eq 'NavScripts'
    $record.nav_qpc = & $now
    $nav.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
    $readyLabel=[regex]::Unescape('\u5f15\u64ce\u5c31\u7eea')
    $null = Await-Startup {
        if ($userClient) {
            $newScript = Find-StartupControl $window $(if ($workPage) { 'RefreshWindows' } else { 'NewScript' })
            return $newScript -and $newScript.Current.IsEnabled
        }
        $health = Find-StartupControl $window 'ServiceStatus'
        $health -and $health.Current.Name -eq $readyLabel
    } 'engine ready'
    $record.ready_seen_qpc = & $now
    $go = Await-Startup {
        if ($userClient -and -not $workPage) {
            $row = Find-StartupControl $window ("Run_" + $scenario.script_id)
            if ($row -and $row.Current.IsEnabled) { return $row }
            return
        }
        $button = Find-StartupControl $window $(if ($workPage) { 'StartWork' } else { 'ExecuteScript' })
        $picker = Find-StartupControl $window $(if ($workPage) { 'WorkScript' } else { 'RunScriptPicker' })
        if ($button -and $picker -and $button.Current.IsEnabled) {
            $selection = $picker.GetCurrentPattern([Windows.Automation.SelectionPattern]::Pattern).Current.GetSelection()
            if ($selection.Count -eq 1 -and $selection[0].Current.Name -eq $scenario.name) { $button }
        }
    } 'unique selected script and enabled ExecuteScript'
    $record.selected_qpc = & $now
    $record.name = $scenario.name
    $previous = ''
    $acceptedIds = @{}
    for ($i = 0; $i -lt $scenario.count; $i++) {
        $warmWorker=$null
        if ($i -gt 0 -and $scenario.kind -ne 'lua') {
            [IO.File]::WriteAllText((Join-Path $scenario.fixture "request-$i"), 'show')
            $null = Await-Startup { Test-Path -LiteralPath (Join-Path $scenario.fixture "shown-$i.json") } 'distinct visible input'
            $workerMarks=@(Get-Content -LiteralPath $scenario.timing -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'ocr_inference' -and $_.execution_id -eq $previous })
            if ($workerMarks.Count -ne 1) { throw 'Previous execution lacks a unique inference worker' }
            $workerProcess=Get-Process -Id $workerMarks[0].worker_pid -ErrorAction Stop
            if ($workerProcess.Path -ne $scenario.worker_executable -or $workerProcess.HasExited) { throw 'The model worker exited or changed before the warm request' }
            $warmWorker=[ordered]@{pid=$workerProcess.Id;created=$workerProcess.StartTime.ToUniversalTime().ToString('o');qpc=(& $now)}
        }
        $null = Await-Startup { $go.Current.IsEnabled -and (-not $workPage -or $go.Current.Name -eq '开始') } 'enabled ExecuteScript'
        $begin = & $now
        $attempt=[ordered]@{index=$i;status='invoking';invoke_qpc=$begin;warm_worker=$warmWorker}
        $record.runs += $attempt
        $go.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
        $feedback = if (-not $userClient) { Find-StartupControl $window 'RunFeedback' }
        $id = Await-Startup {
            if ($userClient) {
                $accepted = @(Get-Content -LiteralPath $scenario.timing -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json } |
                    Where-Object { $_.event -eq 'execute_accepted' -and $_.script_id -eq $scenario.script_id -and -not $acceptedIds.ContainsKey($_.execution_id) })
                if ($accepted.Count -gt 1) { throw 'One click created multiple execution IDs' }
                if ($accepted.Count -eq 1) { return $accepted[0].execution_id }
                return
            }
            $m = [regex]::Match($feedback.Current.Name, '[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}')
            if ($m.Success -and $m.Value -ne $previous) { $m.Value }
        } 'new accepted execution'
        $previous = $id
        $acceptedIds[$id] = $true
        $accepted = & $now
        $attempt.execution_id=$id
        $attempt.accepted_seen_qpc=$accepted
        $file = Join-Path $scenario.history ($id+'.json')
        $summary = Await-Startup {
            if (Test-Path -LiteralPath $file) {
                try { $s = Get-Content -LiteralPath $file -Raw -Encoding UTF8 | ConvertFrom-Json
                      if ($s.status -in @('completed','failed','cancelled')) { $s } } catch [IO.IOException] { }
            }
        } 'execution terminal history'
        $attempt.script_id=$summary.script_id
        $attempt.status=$summary.status
        $attempt.error=$summary.error_code
        $attempt.terminal_seen_qpc=(& $now)
        if ($summary.status -ne 'completed' -or $summary.script_id -ne $scenario.script_id) { throw "Script ended $($summary.status) / $($summary.error_code)" }
    }
    $record.status = 'completed'
    $window.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (-not $process.WaitForExit(15000)) { throw 'Measured application did not exit normally' }
    $record.closed_qpc = & $now
}
