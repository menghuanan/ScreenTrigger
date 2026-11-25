# Runs within verify-workspace.ps1. All windows and records are private to this test.
function Select-ComboItem([string]$id,[string]$name) {
    $box=Element $id
    $expand=$box.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
    $named=[Windows.Automation.PropertyCondition]::new($ae::NameProperty,$name)
    $option=[Windows.Automation.AndCondition]::new($named,[Windows.Automation.PropertyCondition]::new($ae::ControlTypeProperty,[Windows.Automation.ControlType]::ListItem))
    $row=Wait-Until {
        if ($expand.Current.ExpandCollapseState -eq [Windows.Automation.ExpandCollapseState]::Collapsed) { $expand.Expand() }
        $item=$box.FindFirst($tree,$named)
        if (!$item) { $item=$main.FindFirst($tree,$option) }
        if ($item -and $item.Current.IsEnabled) { $item }
    } $name
    Select-Element $row
}
function Test-ReleaseWorkspace {
    Select-Element (Element NavImages)
    & .venv/Scripts/python.exe -X utf8 tests/acceptance/overlay_boundaries.py $app.Id $hwnd.ToInt64() $folder.FullName
    if ($LASTEXITCODE) {throw 'Real overlay boundary or focus assertion failed'}
    $observations.minimum=Wait-Text ImageStatus '10 × 10'
    Select-Element (Element NavScripts)
    Invoke-Named '新建脚本'
    Set-Value ScriptName 'P3 命中动作'
    $lua=@'
local result=args.match
local hit=result.matches[1]
assert(result.found and hit.sample_no==2 and hit.target_id==result.target_id)
assert(hit.evidence[1].matched and hit.bbox.width==80 and hit.bbox.height==60)
log("P3-MATCH "..hit.target_id.." #"..hit.sample_no.." "..hit.bbox.x..","..hit.bbox.y.." "..hit.evidence[1].condition_id)
return hit.sample_no
'@
    Set-Value ScriptSource $lua
    (Find-Element '参数声明' -Name).GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern).Expand()
    Set-Value Parameters '[{"name":"match","type":"object","required":true,"default":null,"description":"识别结果"}]'
    Invoke-Element SaveScript
    $observations.actionSaved=Wait-Text ScriptFeedback '修订 1'
    Select-Element (Element NavMonitor)
    Select-ComboItem Targets 'P1 renamed'
    Select-ComboItem Hit '调用脚本'
    Select-ComboItem ActionScript 'P3 命中动作'
    $python=(Resolve-Path .venv/Scripts/python.exe).Path
    $fixture=Start-Process -FilePath $python -ArgumentList @('-X','utf8','tests/acceptance/show_fixture.py',('"'+$folder.FullName+'"')) -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $folder.FullName 'fixture-stderr.log')
    try {
        $null=Wait-Until { if ($fixture.HasExited) {throw 'Dedicated image window exited'}; Test-Path -LiteralPath (Join-Path $folder.FullName 'fixture-visible.json') } 'pixel-verified image in the dedicated fixture'
        Invoke-Element StartMonitor
        $observations.recognizedAction=Wait-Text MonitorObservation '已完成.*命中 #2'
        Select-Element (Element NavHistory)
        $logFiles=Wait-Until {
            $matches=@(Get-ChildItem -LiteralPath $env:SIDECAR_DATA_DIR -Filter '*.jsonl' -Recurse -File | Select-String -Pattern 'P3-MATCH ')
            if ($matches.Count) { $matches }
        } 'monitor Lua action reached the history writer'
        $observations.actionLog=@($logFiles | ForEach-Object {$_.Line})
        $cat=Join-Path $env:SIDECAR_DATA_DIR 'catalog'
        $ptr=Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $cat 'current.json')|ConvertFrom-Json
        $targets=Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $cat ("generations/"+$ptr.generation+"/targets.json"))|ConvertFrom-Json
        $item=@($targets.targets | Where-Object sample_no -eq 2)[0]
        if (!$item.id -or ($observations.actionLog -join ' ') -notmatch ([regex]::Escape("P3-MATCH "+$item.id+" #2 100,150 c1"))) {
            throw 'Action did not record the saved target ID, expected desktop position and matching condition'
        }
        $observations.stable_match=@{target_id=$item.id;sample_no=2;x=100;y=150;condition='c1'}
    } finally {
        [IO.File]::WriteAllText((Join-Path $folder.FullName 'fixture-stop'),'done')
        if (!$fixture.WaitForExit(7000)) {throw 'Image fixture did not exit'}
    }
}
