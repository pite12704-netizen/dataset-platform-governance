# 【学习标注：共享开发配置】调用与 Vite 相同的解析器，避免 PowerShell 和前端各维护一套地址规则。
function Get-DevelopmentConfig([string]$Workspace, [string]$ConfigPath) {
    $nodeCommand = Get-Command node.exe -ErrorAction Stop
    if (-not $ConfigPath) {
        $ConfigPath = $env:DP_DEV_CONFIG
    }
    if (-not $ConfigPath) {
        $ConfigPath = Join-Path $Workspace 'config\development.json'
    }
    if (-not [IO.Path]::IsPathRooted($ConfigPath)) {
        $ConfigPath = Join-Path $Workspace $ConfigPath
    }
    $resolvedPath = [IO.Path]::GetFullPath($ConfigPath)
    $parser = Join-Path $Workspace 'frontend\dev-config.mjs'
    $result = & $nodeCommand.Source $parser $resolvedPath
    if ($LASTEXITCODE -ne 0) { throw "Cannot read development configuration: $resolvedPath" }
    $config = $result | ConvertFrom-Json
    $config | Add-Member -NotePropertyName ConfigPath -NotePropertyValue $resolvedPath
    return $config
}
