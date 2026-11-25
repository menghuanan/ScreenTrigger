param([Parameter(Mandatory)][string]$Media,[Parameter(Mandatory)][string]$OutputDirectory,
    [Parameter(Mandatory)][string]$WorkDirectory)
$ErrorActionPreference='Stop'
$mediaDir=(Resolve-Path -LiteralPath $Media).Path
$repo=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$work=[IO.Path]::GetFullPath($WorkDirectory)
if (!$work.StartsWith(($repo+'\dist\p3\'),[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $work)) {
    throw 'Supply a new disposable directory under dist/p3'
}
$record=(New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop).FullName
$null=New-Item -ItemType Directory -Path $work
$env:LOCALAPPDATA=$work
$program=Join-Path $work 'program'
$menu=Join-Path $work 'menu'
$data=Join-Path $work 'ScreenTrigger'
& .venv/Scripts/python.exe -X utf8 (Join-Path $repo 'tools/probes/windows/p2/seed.py') $data
if ($LASTEXITCODE) { throw 'Create isolated fixture failed' }
$before=@(Get-ChildItem -File -Recurse -LiteralPath $data | Get-FileHash | Select-Object Path,Hash)
$results=[ordered]@{}
foreach ($action in 'retain','purge') {
    & (Join-Path $mediaDir 'Install.ps1') -InstallRoot $program -DataRoot $data -ShortcutDir $menu
    $state=Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $program 'installation.json') | ConvertFrom-Json
    $version=Join-Path (Join-Path $program 'versions') $state.versions[-1]
    $shortcut=Join-Path $menu 'ScreenTrigger.lnk'
    $target=(New-Object -ComObject WScript.Shell).CreateShortcut($shortcut).TargetPath
    if ($target -ne (Join-Path $version 'ScreenTrigger.exe')) { throw 'Unexpected shortcut target' }
    $script=Join-Path $version 'Uninstall.ps1'
    if ($action -eq 'retain') {
        & $script
        foreach ($f in $before) { if ((Get-FileHash -LiteralPath $f.Path).Hash -ne $f.Hash) { throw 'Original data changed after uninstall' } }
        $results.retain=@{remainingOriginalFiles=$before.Count; shortcut=$target}
    } else {
        $wrong=Join-Path $work 'different'
        try { & $script -DeleteDataRoot $wrong; throw 'Wrong data directory was accepted' }
        catch { if ($_.Exception.Message -notmatch 'exact installed') { throw } }
        & $script -DeleteDataRoot $data
        if (Test-Path -LiteralPath $data) { throw 'Explicit purge left data' }
        $results.purge=@{rejectedWrongDirectory=$true; removedExactFixture=$true}
    }
    if ((Test-Path -LiteralPath $program) -or (Test-Path -LiteralPath $shortcut)) { throw 'Program or shortcut survived uninstall' }
}
$results | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $record 'result.json') -Encoding UTF8
