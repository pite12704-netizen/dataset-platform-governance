# 【学习标注：停止服务】只终止命令行中包含本项目绝对路径的 Python/Node 进程。
$ErrorActionPreference = 'Stop'
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtime = Join-Path $workspace 'var\run'

# Windows venv launchers may exit while their Python child keeps serving.
# Verify the executable and exact workspace entry point instead of trusting a stale PID.
$backendPath = Join-Path $workspace 'backend'
$vitePath = Join-Path $workspace 'frontend\node_modules\vite\bin\vite.js'
$candidates = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe'" -ErrorAction Stop
foreach ($process in $candidates) {
    $command = $process.CommandLine
    if (-not $command) { continue }
    $isBackend = $process.Name -eq 'python.exe' -and $command.Contains($backendPath) -and
        $command.Contains('-m uvicorn') -and $command.Contains('app.main:create_app')
    $isFrontend = $process.Name -eq 'node.exe' -and $command.Contains($vitePath)
    if ($isBackend -or $isFrontend) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped platform process $($process.ProcessId)."
    }
}
foreach ($name in @('backend', 'frontend')) {
    $pidFile = Join-Path $runtime "$name.pid"
    if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force }
}
