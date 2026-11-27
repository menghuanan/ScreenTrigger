[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.venv/Scripts/python.exe'))) {
    throw '请先按开发规范准备项目的 Python 3.13 开发环境。'
}
$env:SCREENTRIGGER_DEV_ROOT = $projectRoot
if (-not $env:SIDECAR_DATA_DIR) {
    $env:SIDECAR_DATA_DIR = Join-Path $projectRoot 'dist/dev-panel/data'
}
dotnet run --project (Join-Path $projectRoot 'desktop/ScreenTrigger/ScreenTrigger.csproj') -c Debug -p:RestoreLockedMode=true
exit $LASTEXITCODE
