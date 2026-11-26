# Windows PowerShell 5.1 and older system policies can limit ordinary Win32
# paths even though the application has a normal-length installation root.
function Get-SetupIoPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\?\')) { return $full }
    if ($full.StartsWith('\\')) { return '\\?\UNC\' + $full.Substring(2) }
    return '\\?\' + $full
}

function Assert-PlainPath([string]$Path) {
    $item = [IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path))
    while ($item) {
        $physical = Get-SetupIoPath $item.FullName
        if ([IO.Directory]::Exists($physical) -and
            ([IO.File]::GetAttributes($physical) -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Linked directory: $($item.FullName)"
        }
        $item = $item.Parent
    }
}

function Assert-PlainSetupTree([string]$Path) {
    Assert-PlainPath $Path
    $pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $pending.Push([IO.DirectoryInfo]::new((Get-SetupIoPath $Path)))
    while ($pending.Count) {
        foreach ($item in $pending.Pop().EnumerateFileSystemInfos()) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Linked content: $($item.FullName)"
            }
            if ($item -is [IO.DirectoryInfo]) { $pending.Push($item) }
        }
    }
}

function Remove-PlainSetupTree([string]$Path) {
    # The caller verifies the owning receipt/parent before allowing this removal.
    Assert-PlainSetupTree $Path
    $full = Get-SetupIoPath $Path
    $files = [IO.Directory]::EnumerateFiles($full, '*', [IO.SearchOption]::AllDirectories)
    foreach ($file in $files) {
        $attrs = [IO.File]::GetAttributes($file)
        if ($attrs -band [IO.FileAttributes]::ReadOnly) {
            [IO.File]::SetAttributes($file, ($attrs -band (-bnot [IO.FileAttributes]::ReadOnly)))
        }
    }
    [IO.Directory]::Delete($full, $true)
}
