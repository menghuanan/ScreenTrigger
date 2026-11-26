[CmdletBinding()]
param([string]$DeleteDataRoot)
$ErrorActionPreference = 'Stop'
$program = (Get-Item -LiteralPath $PSScriptRoot).Parent.Parent.FullName
$versions = Join-Path $program 'versions'
. (Join-Path $PSScriptRoot 'SetupPaths.ps1')
Assert-PlainPath $versions
$record = Join-Path $program 'installation.json'
$state = Get-Content -Raw -Encoding UTF8 -LiteralPath $record | ConvertFrom-Json
if ($state.product -ne 'ScreenTrigger' -or $state.versions -notcontains (Split-Path $PSScriptRoot -Leaf)) { throw 'Unrecognized installation.' }
if ($DeleteDataRoot) {
    $data = [IO.Path]::GetFullPath($DeleteDataRoot).TrimEnd('\')
    Assert-PlainPath $data
    if ($data -ne $state.dataRoot -or $data -eq [IO.Path]::GetPathRoot($data).TrimEnd('\') -or $data -eq $env:USERPROFILE -or $data -eq $env:LOCALAPPDATA) { throw 'Specify the exact installed ScreenTrigger data directory.' }
    $known = @('config.json','catalog','images','scripts','backups','models','history','logs','cache','tmp')
    if ((Get-Item -LiteralPath $data).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing to remove a linked data directory.' }
    if (Get-ChildItem -Force -LiteralPath $data | Where-Object { $_.Name -notin $known }) { throw 'Data directory contains additional files. Review it manually.' }
    Assert-PlainSetupTree $data
}
$lock = [IO.File]::Open((Join-Path $program '.setup.lock'),'OpenOrCreate','ReadWrite','None')
try {
    foreach ($name in $state.versions) {
        if ($name -notmatch '^[a-f0-9]{32}$') { throw 'Invalid version directory.' }
        $dir = [IO.Path]::GetFullPath((Join-Path $versions $name))
        if ((Split-Path $dir -Parent) -ne $versions) { throw 'Invalid version directory.' }
        if (!(Test-Path -LiteralPath $dir)) { continue }
        if ((Get-Item -LiteralPath $dir).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked version directory.' }
        Assert-PlainSetupTree $dir
        $running = Get-Process ScreenTrigger -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Join-Path $dir 'ScreenTrigger.exe') }
        if ($running) { throw 'Close ScreenTrigger before uninstalling.' }
    }
    foreach ($name in $state.versions) {
        $dir = [IO.Path]::GetFullPath((Join-Path $versions $name))
        if ((Split-Path $dir -Parent) -ne $versions) { throw 'Invalid version directory.' }
        if (Test-Path -LiteralPath $dir) { Remove-PlainSetupTree $dir }
    }
    $shortcut = Join-Path $state.shortcutDir 'ScreenTrigger.lnk'
    if (Test-Path -LiteralPath $shortcut) {
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut).TargetPath
        if ($target.StartsWith(($versions + '\'), [StringComparison]::OrdinalIgnoreCase)) { Remove-Item -LiteralPath $shortcut }
    }
    if ($DeleteDataRoot) { Remove-PlainSetupTree $data }
    foreach ($file in @($record,(Join-Path $program 'installation.previous'))) {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file }
    }
} finally { $lock.Dispose() }
Remove-Item -LiteralPath (Join-Path $program '.setup.lock')
if (!(Get-ChildItem -Force -LiteralPath $versions)) { [IO.Directory]::Delete($versions) }
if (!(Get-ChildItem -Force -LiteralPath $program)) { [IO.Directory]::Delete($program) }
