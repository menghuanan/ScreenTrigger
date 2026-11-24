param(
    [ValidateSet('baseline','P0','P1','P2','P3','release')][string]$Scope = 'baseline',
    [string]$Task = '',
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$AuditArgs = @((Join-Path $PSScriptRoot 'run_acceptance.py'), 'audit', '--scope', $Scope)
if ($Task) { $AuditArgs += @('--task', $Task) }
& $Python @AuditArgs
exit $LASTEXITCODE
