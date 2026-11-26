param([Parameter(Mandatory)][string]$Executable,[Parameter(Mandatory)][string]$OutputDirectory,
    [int]$Seconds=7200)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$folder=(New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop).FullName
$env:SIDECAR_DATA_DIR=Join-Path $folder 'data'
$exe=(Resolve-Path -LiteralPath $Executable).Path
$python=Join-Path (Split-Path $exe -Parent) 'resources/python/python.exe'
$seed=@'
import sys
from backend.main import SidecarArgs,build_catalog_services
from backend.image.store import SessionImageStore
repo,targets,scripts=build_catalog_services(SidecarArgs("soak-seed",data_dir=sys.argv[1]),SessionImageStore("soak-seed"))
scripts.save_script("Windows 长稳","local n=0\nwhile true do\n local t={}\n for i=1,128 do t[i]=string.rep('r',256) end\n n=n+#t\n wait(0.25)\nend",[])
repo.close()
'@
& $python -I -B -X utf8 -c $seed $env:SIDECAR_DATA_DIR
if ($LASTEXITCODE) {throw 'Cannot seed isolated catalog'}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SoakWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr window, int command);
}
'@
$ae=[Windows.Automation.AutomationElement]
$tree=[Windows.Automation.TreeScope]::Descendants
function Find-Own([string]$id) {
    $main.FindFirst($tree,[Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty,$id))
}
function Await([scriptblock]$read,[string]$name) {
    $end=[datetime]::UtcNow.AddSeconds(60)
    do {
        if ($app.HasExited) { throw "Application exited while waiting for $name" }
        $v=& $read
        if ($v) {return $v}
        Start-Sleep -Milliseconds 125
    } while ([datetime]::UtcNow -lt $end)
    throw "Timeout waiting for $name"
}
function Invoke-Own([string]$id) {
    $control=Await { $c=Find-Own $id; if ($c.Current.IsEnabled) {$c} } $id
    $control.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
}
function Descendants([int]$parent) {
    foreach ($child in (Get-CimInstance Win32_Process -Filter "ParentProcessId=$parent")) {
        [int]$child.ProcessId
        Descendants ([int]$child.ProcessId)
    }
}
$app=Start-Process -FilePath $exe -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $folder 'stderr.log')
try {
    $handle=Await {$app.Refresh(); if ($app.MainWindowHandle -ne [IntPtr]::Zero) {$app.MainWindowHandle}} 'main window'
    $main=$ae::FromHandle($handle)
    $null=Await { $c=Find-Own StopEngine; if ($c.Current.IsEnabled) {$c} } 'engine'
    $nav=Find-Own NavRun
    if (!$nav -or $nav.Current.IsOffscreen) { Invoke-Own TopNavOverflowButton; $nav=Find-Own NavRun }
    $select=$null
    if ($nav.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern,[ref]$select)) {$select.Select()}
    else {$nav.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()}
    Invoke-Own ExecuteScript
    $status=Await {$t=(Find-Own RunStatus).Current.Name; if ($t -match '运行中') {$t}} 'active Lua script'
    [SoakWindow]::ShowWindow($handle,6) | Out-Null
    $writer=[IO.StreamWriter]::new((Join-Path $folder 'processes.jsonl'),$false,[Text.UTF8Encoding]::new($false))
    $watch=[Diagnostics.Stopwatch]::StartNew()
    try {
        while ($true) {
            $app.Refresh()
            if ($app.HasExited) {throw 'Application exited during soak'}
            $ids=@($app.Id)+@(Descendants $app.Id)
            $processes=@(Get-Process -Id $ids | ForEach-Object {
                [pscustomobject]@{id=$_.Id;name=$_.ProcessName;rss=$_.WorkingSet64;privateBytes=$_.PrivateMemorySize64}
            })
            if ($processes.Count -lt 2) {throw 'Engine process tree lost'}
            $rss=($processes|Measure-Object -Property rss -Sum).Sum
            if (!$rss -or $rss -le 0) {throw 'Missing process tree RSS sample'}
            $writer.WriteLine(([ordered]@{elapsed=$watch.Elapsed.TotalSeconds;rss=$rss;processes=$processes}|ConvertTo-Json -Compress -Depth 4))
            $writer.Flush()
            if ($watch.Elapsed.TotalSeconds -ge $Seconds) {break}
            Start-Sleep -Seconds ([Math]::Min(15,[Math]::Ceiling($Seconds-$watch.Elapsed.TotalSeconds)))
        }
    } finally {$writer.Dispose()}
    [SoakWindow]::ShowWindow($handle,9) | Out-Null
    $before=(Find-Own RunStatus).Current.Name
    if ($before -notmatch '运行中') {throw "Unexpected terminal before stop: $before"}
    Invoke-Own StopExecution
    $terminal=Await {$t=(Find-Own RunStatus).Current.Name;if ($t -match '已取消') {$t}} 'cancelled'
    $main.GetCurrentPattern([Windows.Automation.WindowPattern]::Pattern).Close()
    if (!$app.WaitForExit(15000) -or $app.ExitCode) {throw 'Unclean WinUI exit'}
    $measure=@(Get-Content -Encoding UTF8 -LiteralPath (Join-Path $folder 'processes.jsonl') | ForEach-Object {$_|ConvertFrom-Json})
    $result=[ordered]@{requested_s=$Seconds;observed_s=$watch.Elapsed.TotalSeconds;samples=$measure.Count;status=$status;beforeStop=$before;terminal=$terminal;exit=$app.ExitCode}
    $histories=@(Get-ChildItem -LiteralPath (Join-Path $env:SIDECAR_DATA_DIR 'history') -File -Filter '*.json')
    if ($histories.Count -ne 1) {throw 'Expected exactly one root execution in isolated history'}
    $history=Get-Content -Encoding utf8 -Raw -LiteralPath $histories[0].FullName | ConvertFrom-Json
    $events=@(Get-Content -Encoding utf8 -LiteralPath ([IO.Path]::ChangeExtension($histories[0].FullName,'jsonl')) |
        ForEach-Object {$_ | ConvertFrom-Json} | Where-Object {$_.event -eq 'script_state_changed'})
    $states=@($events | ForEach-Object {$_.message})
    if ($history.status -ne 'cancelled' -or $history.reason -ne 'user_requested' -or $history.error_code -or
        @($events | Where-Object {$_.execution_id -ne $history.execution_id -or $_.details.error_code}).Count -or
        ($states -join ',') -ne 'starting,running,stopping,cancelled') {
        throw "Unexpected root history for $($history.execution_id): $states"
    }
    $runningAt=[datetimeoffset]::Parse($events[1].timestamp)
    $stoppingAt=[datetimeoffset]::Parse($events[2].timestamp)
    $result.executionId=$history.execution_id
    $result.runningSeconds=($stoppingAt-$runningAt).TotalSeconds
    $result.terminalReason=$history.reason
    if ($result.runningSeconds -lt $Seconds) {throw 'Execution duration was shorter than requested'}
    if ($Seconds -ge 7200) {
        $early=@($measure|Where-Object {$_.elapsed -ge 300 -and $_.elapsed -lt 2100}|Sort-Object rss)
        $late=@($measure|Where-Object {$_.elapsed -ge ($Seconds-1800)}|Sort-Object rss)
        $a=$early[[int][Math]::Floor($early.Count/2)].rss
        $b=$late[[int][Math]::Floor($late.Count/2)].rss
        $result.earlyMedianRss=$a; $result.lateMedianRss=$b; $result.growth=($b-$a)/$a
    }
    $result|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $folder 'result.json') -Encoding UTF8
    if ($result.growth -gt 0.10) {throw 'Resident set growth exceeds 10%'}
} finally {
    if (!$app.HasExited) {
        $app.CloseMainWindow()|Out-Null
        if (!$app.WaitForExit(5000)) {$app.Kill()}
    }
}
