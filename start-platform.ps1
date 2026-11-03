param(
    [ValidateSet('start','status','doctor','stop','backup','create-admin','reload-app')][string]$Action = 'start',
    [switch]$Build,
    [string]$Username,
    [string]$Bundle
)
# This entry works regardless of the shell's current directory.
& (Join-Path $PSScriptRoot 'scripts/enterprise.ps1') -Action $Action -Build:$Build -Username $Username -Bundle $Bundle
