[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\ScreenTrigger'),
    [string]$DataRoot,
    [string]$ShortcutDir = [Environment]::GetFolderPath('Programs')
)
$ErrorActionPreference = 'Stop'
function Assert-PlainPath([string]$Path) {
    $item = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path))
    while ($item) {
        if ($item.Exists -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Linked directory: $($item.FullName)" }
        $item = $item.Parent
    }
}
if (![Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
$program = [IO.Path]::GetFullPath($InstallRoot)
$manifest = Join-Path $PSScriptRoot 'package.json'
$build = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
$media = Join-Path $PSScriptRoot 'app'
$receipt = Join-Path $program 'installation.json'
$prior = if (Test-Path -LiteralPath $receipt) { Get-Content -Raw -Encoding UTF8 -LiteralPath $receipt | ConvertFrom-Json }
if ((Test-Path -LiteralPath $program) -and !$prior -and @(Get-ChildItem -Force -LiteralPath $program).Count) {
    throw "Choose an empty install directory: $program"
}
if ($prior -and $prior.product -ne 'ScreenTrigger') { throw 'Unrecognized installation record.' }
if ($prior -and !$PSBoundParameters.ContainsKey('ShortcutDir')) { $ShortcutDir = $prior.shortcutDir }
$defaultRoot = Join-Path $env:LOCALAPPDATA 'ScreenTrigger'
$legacy = Join-Path $env:LOCALAPPDATA 'com.screentrigger.app'
$requested = @($DataRoot, $prior.dataRoot, $defaultRoot, $legacy) | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_) } | Select-Object -Unique
$metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifest | ConvertFrom-Json
$versionRoot = Join-Path $program 'versions'
Assert-PlainPath $versionRoot
[IO.Directory]::CreateDirectory($versionRoot) | Out-Null
$mutexFile = [IO.File]::Open((Join-Path $program '.setup.lock'),'OpenOrCreate','ReadWrite','None')
$stage = Join-Path $versionRoot ([guid]::NewGuid().ToString('N'))
$installed = $false
try {
    $null = [IO.Directory]::CreateDirectory($stage)
    foreach ($file in $metadata.files) {
        $target = [IO.Path]::GetFullPath((Join-Path $stage $file.path))
        if (!$target.StartsWith(($stage + '\'), [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid package path.' }
        $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
        [IO.File]::Copy((Join-Path $media $file.path), $target)
        $inputStream = [IO.File]::OpenRead($target)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $hash = [BitConverter]::ToString($hasher.ComputeHash($inputStream)).Replace('-','')
            if ($inputStream.Length -ne $file.bytes -or $hash -ne $file.sha256) { throw "Package verification failed: $($file.path)" }
        } finally { $inputStream.Dispose(); $hasher.Dispose() }
    }
    $engine = Join-Path $stage 'resources\python\python.exe'
    $inspect = Join-Path $stage 'resources\inspect_root.py'
    $existing = @()
    foreach ($root in $requested) {
        if (!(Test-Path -LiteralPath $root)) { continue }
        Assert-PlainPath $root
        $json = & $engine -I -B $inspect $root
        if ($LASTEXITCODE) { throw "Data verification failed: $root" }
        $info = $json | ConvertFrom-Json
        if ($info.existing) { $existing += $info }
    }
    if ($existing.Count -gt 1) {
        for ($i=0; $i -lt $existing.Count; $i++) { Write-Host "[$i] $($existing[$i].path)  targets=$($existing[$i].targets) scripts=$($existing[$i].scripts)" }
    }
    if ($DataRoot) { $selected = [IO.Path]::GetFullPath($DataRoot) }
    elseif ($prior) { $selected = $prior.dataRoot }
    elseif ($existing.Count -gt 1) {
        $choice = -1
        if (![int]::TryParse((Read-Host 'Select the data root to retain'),[ref]$choice) -or $choice -lt 0 -or $choice -ge $existing.Count) { throw 'Installation cancelled.' }
        $selected = $existing[$choice].path
    } elseif ($existing.Count) { $selected = $existing[0].path }
    else { $selected = $defaultRoot }
    Assert-PlainPath $selected
    if ($selected -eq $program -or $selected.StartsWith(($program + '\'),[StringComparison]::OrdinalIgnoreCase) -or $program.StartsWith(($selected.TrimEnd('\') + '\'),[StringComparison]::OrdinalIgnoreCase)) { throw 'Install and data directories must be separate.' }
    if ($existing.Count -gt 1) {
        foreach ($data in $existing) {
            $copy = "$($data.path).pre-upgrade-$([guid]::NewGuid().ToString('N'))"
            $null = & $engine -I -B $inspect $data.path --copy-to $copy
            if ($LASTEXITCODE) { throw "Upgrade backup verification failed: $copy" }
            Write-Host "Preserved $copy"
        }
    }
    [IO.File]::WriteAllText((Join-Path $stage 'data-root.txt'), $selected)
    Copy-Item -LiteralPath $manifest -Destination (Join-Path $stage 'package.json')
    $targetExe = Join-Path $stage $metadata.entry
    $shell = New-Object -ComObject WScript.Shell
    $null = [IO.Directory]::CreateDirectory($ShortcutDir)
    $shortcutPath = Join-Path $ShortcutDir 'ScreenTrigger.lnk'
    $link = $shell.CreateShortcut($shortcutPath)
    $link.TargetPath = $targetExe
    $link.WorkingDirectory = $stage
    $versions = @(@($prior.versions) | Where-Object { $_ }) + @(Split-Path $stage -Leaf)
    $state = @{product='ScreenTrigger';version=$build;versions=$versions;dataRoot=$selected;shortcutDir=$ShortcutDir}
    $tmp = Join-Path $program 'installation.tmp'
    [IO.File]::WriteAllText($tmp, ($state | ConvertTo-Json))
    if ($prior) { [IO.File]::Replace($tmp,$receipt,(Join-Path $program 'installation.previous')) }
    else { [IO.File]::Move($tmp,$receipt) }
    try { $link.Save() }
    catch {
        if ($prior) { [IO.File]::Replace((Join-Path $program 'installation.previous'),$receipt,$null) }
        else { [IO.File]::Delete($receipt) }
        throw
    }
    $installed = $true
    Write-Output "Installed $targetExe"
    Write-Output "Data $selected"
} finally {
    $mutexFile.Dispose()
    $target = [IO.Path]::GetFullPath($stage)
    if (!$installed -and (Split-Path $target -Parent) -eq $versionRoot -and (Test-Path -LiteralPath $target)) {
        Assert-PlainPath $target
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
