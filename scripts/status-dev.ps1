param([string]$ConfigPath)
# 【学习标注：状态检查】只请求健康地址，不修改服务或数据。
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$activePath = Join-Path $workspace 'var/run/active-deployment.json'
if (Test-Path -LiteralPath $activePath) {
    $profile = Get-Content -Raw -LiteralPath $activePath | ConvertFrom-Json
    $env:DP_SETTINGS_FILE = $profile.host_env
}
. (Join-Path $PSScriptRoot 'dev-config.ps1')
$dev = Get-DevelopmentConfig -Workspace $workspace -ConfigPath $ConfigPath

function Get-EndpointStatus([string]$Name, [string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
        [PSCustomObject]@{ Service = $Name; Status = 'Running'; HTTP = $response.StatusCode; Address = $Url }
    } catch {
        [PSCustomObject]@{ Service = $Name; Status = 'Stopped'; HTTP = '-'; Address = $Url }
    }
}

@(
    Get-EndpointStatus 'Backend API' "$($dev.apiUrl)/health/ready"
    Get-EndpointStatus 'Frontend' "$($dev.webUrl)/"
) | Format-Table -AutoSize

# 模型失败单独显示，不能由网站 HTTP 200 推断推理服务就绪。
$python = Join-Path $workspace '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $python) {
    $modelStatus = & $python (Join-Path $workspace 'scripts\model_health.py')
    if ($LASTEXITCODE -eq 0) {
        $modelStatus | ConvertFrom-Json | Format-Table service, status, model, seconds -AutoSize
    } else {
        Write-Warning 'Cannot read model status; check backend configuration.'
    }
}
