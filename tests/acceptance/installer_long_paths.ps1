# Minimal installer fixture for the old Windows PowerShell MAX_PATH regression.
# Run with Windows PowerShell 5.1. Every created file and shortcut belongs to this attempt.
param([Parameter(Mandatory)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$folder = [IO.Path]::GetFullPath($OutputDirectory)
if (!$folder.StartsWith(($repo + '\reports\raw\'), [StringComparison]::OrdinalIgnoreCase) -or
    (Test-Path -LiteralPath $folder)) { throw 'Use a new test directory under reports/raw' }
$null = [IO.Directory]::CreateDirectory($folder)
$media = Join-Path $folder 'media'
$app = Join-Path $media 'app'
$null = [IO.Directory]::CreateDirectory($app)
$source = Join-Path $repo 'tools/packaging'
Copy-Item -LiteralPath (Join-Path $source 'Install.ps1') -Destination $media
foreach ($name in 'SetupPaths.ps1','Uninstall.ps1') {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination $app
}
. (Join-Path $app 'SetupPaths.ps1')
$env:LOCALAPPDATA = Join-Path $folder 'fixture-profile'
$program = Join-Path $env:LOCALAPPDATA 'Programs\ScreenTrigger'
$shortcutDir = Join-Path $folder 'shortcuts'
$data = Join-Path $env:LOCALAPPDATA 'ScreenTrigger'
$deep = 'resources/python/Lib/site-packages/modelscope/msdatasets/dataset_cls/custom_datasets/image_quality_assessment_degradation/image_quality_assessment_degradation_dataset.py'
$longFile = Join-Path $app $deep
$null = [IO.Directory]::CreateDirectory((Get-SetupIoPath ([IO.Path]::GetDirectoryName($longFile))))
[IO.File]::WriteAllText((Get-SetupIoPath $longFile),'long path fixture payload')
$entry = 'Uninstall.ps1'
$rows = foreach ($name in 'SetupPaths.ps1','Uninstall.ps1',$deep) {
    $target = Get-SetupIoPath (Join-Path $app $name)
    $stream = [IO.File]::OpenRead($target)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        @{path=$name;bytes=$stream.Length;sha256=[BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}
    } finally { $hasher.Dispose(); $stream.Dispose() }
}
$manifest = Join-Path $media 'package.json'
@{format=1;entry=$entry;files=@($rows)} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifest -Encoding UTF8
$receipt = Join-Path $program 'installation.json'
function Install-Fixture {
    & (Join-Path $media 'Install.ps1') -InstallRoot $program -DataRoot $data -ShortcutDir $shortcutDir
}
$goodManifest = [IO.File]::ReadAllBytes($manifest)
$rows[-1].sha256 = '0' * 64
@{format=1;entry=$entry;files=@($rows)} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifest -Encoding UTF8
try { Install-Fixture; throw 'Invalid initial install was accepted' }
catch { if ($_.Exception.Message -notlike '*Package verification failed*') { throw } }
if ((Test-Path -LiteralPath $receipt) -or @(Get-ChildItem -LiteralPath (Join-Path $program 'versions')).Count) {
    throw 'Invalid initial install left a receipt or staging directory'
}
[IO.File]::WriteAllBytes($manifest,$goodManifest)
$observations = @()
foreach ($step in 'fresh','upgrade') {
    Install-Fixture
    $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $receipt | ConvertFrom-Json
    $active = Join-Path (Join-Path $program 'versions') $state.versions[-1]
    $installedFile = Join-Path $active $deep
    if ($installedFile.Length -le 260 -or [IO.File]::ReadAllText((Get-SetupIoPath $installedFile)) -ne 'long path fixture payload') {
        throw "Long installed path failed at $step"
    }
    $observations += @{step=$step;length=$installedFile.Length;installedFile=$installedFile;versions=@($state.versions)}
}
$saved = Get-Content -Raw -Encoding UTF8 -LiteralPath $receipt
[IO.File]::AppendAllText((Get-SetupIoPath $longFile),' invalid')
try { Install-Fixture; throw 'Hash mismatch was accepted' }
catch { if ($_.Exception.Message -notlike '*Package verification failed*') { throw } }
if ((Get-Content -Raw -Encoding UTF8 -LiteralPath $receipt) -ne $saved -or
    @(Get-ChildItem -Directory -LiteralPath (Join-Path $program 'versions')).Count -ne 2) {
    throw 'Failed install changed the active version or left a staging tree'
}
& (Join-Path $active 'Uninstall.ps1')
if ((Test-Path -LiteralPath $program) -or (Test-Path -LiteralPath (Join-Path $shortcutDir 'ScreenTrigger.lnk'))) {
    throw 'Uninstall left the receipt, version root or test-owned shortcut'
}
@{systemLongPathsEnabled=(Get-ItemPropertyValue -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled);
  observations=$observations;retriedFailedInitialInstall=$true;rejectedCorruptUpgrade=$true;cleanedStage=$true;uninstalledBothVersions=$true} |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $folder 'observations.json') -Encoding UTF8
