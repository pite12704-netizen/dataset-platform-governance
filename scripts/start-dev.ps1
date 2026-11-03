param(
    [switch]$NoBrowser,
    [switch]$SkipModel,
    [string]$ConfigPath
)

# 【学习标注：开发启动】检查依赖 → 自动迁移数据库 → 后台启动 API/Vite → 等待健康检查。

$ErrorActionPreference = 'Stop'
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$activePath = Join-Path $workspace 'var/run/active-deployment.json'
if (Test-Path -LiteralPath $activePath) {
    $profile = Get-Content -Raw -LiteralPath $activePath | ConvertFrom-Json
    if ($profile.kind -ne 'fresh-private') { throw 'Invalid active deployment.' }
    # 开发进程明确读取新实例的宿主机配置，不能回落到旧业务 .env。
    $env:DP_SETTINGS_FILE = $profile.host_env
    $env:DP_ENVIRONMENT = 'development'
    $env:DP_COOKIE_SECURE = 'false'
}
. (Join-Path $PSScriptRoot 'dev-config.ps1')
$dev = Get-DevelopmentConfig -Workspace $workspace -ConfigPath $ConfigPath
$runtime = Join-Path $workspace 'var\run'
$logs = Join-Path $workspace 'var\logs'
$python = Join-Path $workspace '.venv\Scripts\python.exe'
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
$vite = Join-Path $workspace 'frontend\node_modules\vite\bin\vite.js'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Project virtual environment is missing. Create .venv and install backend\requirements.txt first."
}
if (-not $node) {
    throw 'node.exe was not found. Install Node.js first.'
}
if (-not (Test-Path -LiteralPath $vite)) {
    throw 'Frontend dependencies are missing. Run npm install in the frontend directory first.'
}

New-Item -ItemType Directory -Force -Path $runtime, $logs | Out-Null

function Test-Endpoint([string]$Url) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-Endpoint "$($dev.apiUrl)/health/live")) {
    Push-Location $workspace
    try {
        & $python -m alembic -c backend/alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed; backend startup was cancelled.' }
    } finally {
        Pop-Location
    }
    $backendDir = Join-Path $workspace 'backend'
    $backendArgs = "-m uvicorn --app-dir `"$backendDir`" app.main:create_app --factory --host $($dev.apiHost) --port $($dev.apiPort) --no-access-log"
    # 子进程环境优先于 .env：同步回调、邮件链接与 CORS；不修改数据库或密钥配置。
    $backendEnvironment = @{
        DP_PUBLIC_URL = $dev.apiUrl
        DP_FRONTEND_URL = $dev.webUrl
        DP_CORS_ORIGINS = ConvertTo-Json -InputObject @($dev.corsOrigins) -Compress
    }
    $previousEnvironment = @{}
    try {
        foreach ($key in $backendEnvironment.Keys) {
            $previousEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
            [Environment]::SetEnvironmentVariable($key, $backendEnvironment[$key], 'Process')
        }
        $backend = Start-Process -FilePath $python `
            -ArgumentList $backendArgs `
            -WorkingDirectory $workspace -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logs 'backend.out.log') `
            -RedirectStandardError (Join-Path $logs 'backend.err.log')
    } finally {
        foreach ($key in $previousEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($key, $previousEnvironment[$key], 'Process')
        }
    }
    Set-Content -LiteralPath (Join-Path $runtime 'backend.pid') -Value $backend.Id
}

if (-not (Test-Endpoint "$($dev.webUrl)/")) {
    $frontendArgs = "`"$vite`""
    $previousConfigPath = $env:DP_DEV_CONFIG
    try {
        $env:DP_DEV_CONFIG = $dev.ConfigPath
        $frontend = Start-Process -FilePath $node `
            -ArgumentList $frontendArgs `
            -WorkingDirectory (Join-Path $workspace 'frontend') -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logs 'frontend.out.log') `
            -RedirectStandardError (Join-Path $logs 'frontend.err.log')
    } finally {
        [Environment]::SetEnvironmentVariable('DP_DEV_CONFIG', $previousConfigPath, 'Process')
    }
    Set-Content -LiteralPath (Join-Path $runtime 'frontend.pid') -Value $frontend.Id
}

$deadline = (Get-Date).AddSeconds(30)
do {
    $apiReady = Test-Endpoint "$($dev.apiUrl)/health/ready"
    $webReady = Test-Endpoint "$($dev.webUrl)/"
    if ($apiReady -and $webReady) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)

if (-not ($apiReady -and $webReady)) {
    Write-Host 'Startup did not finish within 30 seconds. Check var\logs.' -ForegroundColor Red
    exit 1
}

# 【学习标注：独立模型服务】网站就绪不代表模型已加载。按后端实际配置恢复本机模型；失败时网站仍可用。
if (-not $SkipModel) {
    try {
        $modelConfig = & $python (Join-Path $workspace 'scripts\model_health.py') --startup-config
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read model configuration.' }
        $modelPorts = @(($modelConfig | ConvertFrom-Json).local_ports)
        if ($modelPorts.Count -eq 1) {
            & (Join-Path $workspace 'scripts\start-lmstudio.ps1') -Port $modelPorts[0]
        } elseif ($modelPorts.Count -gt 1) {
            throw 'Multiple local model ports configured. LM Studio serves one port; align DP_MODEL_URL and DP_VISION_MODEL_URL.'
        }
    } catch {
        Write-Warning "Model startup failed: $($_.Exception.Message) The data platform remains available."
    }
}

Write-Host 'Dataset Platform is running:' -ForegroundColor Green
Write-Host "  Config: $($dev.ConfigPath)"
Write-Host "  Web: $($dev.webUrl)/"
Write-Host "  API: $($dev.apiUrl)/docs"

if (-not $NoBrowser) {
    Start-Process "$($dev.webUrl)/"
}
