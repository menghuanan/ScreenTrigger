param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
& $Python (Join-Path $PSScriptRoot 'check_docs.py') --output (Join-Path $PSScriptRoot '../reports/docs-validation.json')
exit $LASTEXITCODE
