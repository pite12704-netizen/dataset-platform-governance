param(
    [switch]$ImportModel,
    [ValidateRange(1, 65535)][int]$Port = 1234,
    [string]$StudioPath
)

# LM Studio 的桌面界面与 CLI 使用同一模型库；API 只监听本机。
$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$lms = Join-Path $env:USERPROFILE '.lmstudio\bin\lms.exe'
$modelDirectory = Join-Path $workspace 'var\image-pilot\lmstudio'
$modelFile = 'Qwen3-VL-4B-Instruct-Q4_K_M.gguf'
$projectorFile = 'mmproj-Qwen3-VL-4B-Instruct-F16.gguf'
$repository = 'lmstudio-community/Qwen3-VL-4B-Instruct-GGUF'
$identifier = 'image-pilot-qwen3-vl-4b'
if (-not (Test-Path -LiteralPath $lms)) { throw 'LM Studio CLI is not installed.' }

# 【学习标注：启动依赖】打开过桌面不等于 API 已启动。未运行时从安装信息定位程序，在后台启动。
if (-not (Get-Process -Name 'LM Studio', 'llmster' -ErrorAction SilentlyContinue)) {
    if (-not $StudioPath) {
        $installed = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -like 'LM Studio*' } | Select-Object -First 1
        if ($installed.DisplayIcon) {
            $StudioPath = ($installed.DisplayIcon -replace ',\d+$', '').Trim('"')
        }
    }
    if (-not $StudioPath -or -not (Test-Path -LiteralPath $StudioPath -PathType Leaf)) {
        throw 'Cannot locate LM Studio. Supply -StudioPath with the installed LM Studio.exe path.'
    }
    Start-Process -FilePath $StudioPath -WindowStyle Hidden | Out-Null
}

$deadline = (Get-Date).AddSeconds(30)
do {
    try {
        $loadedJson = & $lms ps --json 2>$null
        $lmsReady = $LASTEXITCODE -eq 0
    } catch {
        $lmsReady = $false
    }
    if ($lmsReady) { break }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $deadline)
if (-not $lmsReady) { throw 'LM Studio did not become ready within 30 seconds.' }

if ($ImportModel) {
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $modelDirectory 'download_manifest.json') | ConvertFrom-Json
    foreach ($filename in @($modelFile, $projectorFile)) {
        $modelPath = Join-Path $modelDirectory $filename
        $expected = $manifest.files.PSObject.Properties[$filename].Value.sha256
        if ((Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw "Model checksum failed: $filename"
        }
        & $lms import $modelPath --user-repo $repository --copy -y
        if ($LASTEXITCODE -ne 0) { throw "LM Studio import failed: $filename" }
    }
}

$loaded = & $lms ps --json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot connect to LM Studio.' }
if (-not ($loaded | Where-Object { $_.identifier -eq $identifier })) {
    $available = & $lms ls --json | ConvertFrom-Json
    $matched = $available | Where-Object { $_.path -eq "$repository/$modelFile" } | Select-Object -First 1
    if (-not $matched) { throw 'The imported vision model is not yet indexed by LM Studio. Retry shortly.' }
    & $lms load $matched.modelKey --gpu max --context-length 8192 --parallel 1 --identifier $identifier -y
    if ($LASTEXITCODE -ne 0) { throw 'Model load failed; verify that both GGUF files were imported.' }
}
& $lms server start --port $Port --bind 127.0.0.1
if ($LASTEXITCODE -ne 0) { throw 'Local model server failed to start.' }
$baseUrl = "http://127.0.0.1:$Port/v1"
$models = Invoke-RestMethod -Uri "$baseUrl/models" -TimeoutSec 10
if (-not ($models.data | Where-Object { $_.id -eq $identifier })) { throw 'Expected model identifier is not available.' }
Write-Host "LM Studio model is available at $baseUrl"
