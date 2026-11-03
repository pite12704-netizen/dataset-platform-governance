param(
    [ValidateSet('start','status','doctor','stop','backup','create-admin','reload-app')][string]$Action = 'status',
    [switch]$LocalAccess,
    [switch]$Desktop,
    [switch]$Build,
    [switch]$BuildOnly,
    [switch]$Legacy,
    [string]$Bundle,
    [string]$Username,
    [string]$SourceEnv = '.env.business.local'
)
# 【学习标注：企业运维】只管理 dataset-platform 项目；停止服务不删除任何数据卷。
$ErrorActionPreference = 'Stop'
if ($BuildOnly -and (-not $Build -or $Action -ne 'start')) {
    throw '-BuildOnly requires -Action start -Build.'
}
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $workspace
$operationLock = $null
try {
    if ($Action -notin @('status','doctor')) {
        $lockDirectory = Join-Path $workspace 'var/run'
        [IO.Directory]::CreateDirectory($lockDirectory) | Out-Null
        try { $operationLock = [IO.File]::Open((Join-Path $lockDirectory 'deployment-operation.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { throw 'Another deployment/backup operation is active. Retry after it finishes.' }
    }
    $python = Join-Path $workspace '.venv\Scripts\python.exe'
    $activePath = Join-Path $workspace 'var/run/active-deployment.json'
    $activeProfile = $null
    $selectedComposeEnv = Join-Path $workspace 'deployment/compose.env.local'
    if (Test-Path -LiteralPath $activePath) {
        $activeProfile = Get-Content -Raw -LiteralPath $activePath | ConvertFrom-Json
        if ($activeProfile.kind -ne 'fresh-private' -or $activeProfile.project -notmatch '^dataset-platform-fresh-[a-zA-Z0-9-]+$') { throw 'Invalid active deployment profile.' }
        $selectedComposeEnv = $activeProfile.compose_env
        $composeArgs = @('compose','-p',$activeProfile.project,'--env-file',$selectedComposeEnv,'-f',$activeProfile.compose_file)
        if (-not $PSBoundParameters.ContainsKey('SourceEnv')) { $SourceEnv = $activeProfile.host_env }
        if ($activeProfile.host_temp_root) {
            if (-not [IO.Path]::IsPathRooted($activeProfile.host_temp_root)) { throw 'Host temporary directory must be an absolute path.' }
            [IO.Directory]::CreateDirectory($activeProfile.host_temp_root) | Out-Null
            $env:TEMP = $activeProfile.host_temp_root
            $env:TMP = $activeProfile.host_temp_root
        }
    } else {
        $instances = Join-Path $env:LOCALAPPDATA 'DatasetPlatform/instances'
        if (-not $Legacy -and (Test-Path -LiteralPath $instances) -and
            @(Get-ChildItem -LiteralPath $instances -Filter profile.json -Recurse -File).Count) {
            throw 'Active instance pointer is missing. Restore var/run/active-deployment.json from the intended instance profile.json; use -Legacy only to deliberately manage the old deployment.'
        }
        $composeArgs = @('compose','--env-file',$selectedComposeEnv,'-f','deployment/compose.yml')
        if ($LocalAccess) { $composeArgs += @('-f','deployment/compose.local.yml') }
        if (Test-Path -LiteralPath (Join-Path $workspace 'deployment/secrets/smtp_password')) {
            $composeArgs += @('-f', 'deployment/compose.smtp.yml')
        }
        if ($Desktop) {
            & $python scripts/stage_desktop_deployment.py
            if ($LASTEXITCODE -ne 0) { throw 'Desktop configuration failed.' }
            $composeArgs += @('-f','deployment/compose.desktop.local')
        }
    }
    function Invoke-Compose([string[]]$Arguments) {
        & docker @composeArgs @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Compose action failed: $($Arguments[0])" }
    }
    if ($Action -in @('start','reload-app')) {
        & $python scripts/storage_preflight.py
        if ($LASTEXITCODE -ne 0) { throw 'Host storage preflight blocked this action; no service was started or rebuilt.' }
    }
    switch ($Action) {
        'doctor' {
            & docker info --format 'Docker {{.ServerVersion}} / {{.OSType}}'
            if ($LASTEXITCODE -ne 0) { throw 'Docker Linux engine is unavailable. Start Docker Desktop and wait for the engine to become ready.' }
            if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment missing. Create .venv and install backend requirements.' }
            if ($activeProfile) {
                foreach ($path in @($activeProfile.compose_file,$activeProfile.compose_env,$activeProfile.host_env)) {
                    if (-not (Test-Path -LiteralPath $path)) { throw 'An active instance configuration file is missing; restore that instance configuration.' }
                }
                Write-Host "Instance: $($activeProfile.project)"
                Write-Host "Website: $($activeProfile.site_url)"
            }
            Invoke-Compose @('ps')
        }
        'start' {
            # 已部署镜像可按 sha256 ID 固定，但 BuildKit 不能把新镜像导出为 sha256:... 标签。
            # 构建时临时使用本地标签，成功后将新镜像 ID 写回仅本机使用的配置。
            $composeEnvPath = $selectedComposeEnv
            $originalEnv = [IO.File]::ReadAllText($composeEnvPath)
            $imageTags = @{
                DP_APP_IMAGE = 'dataset-platform-api:enterprise'
                DP_WEB_IMAGE = 'dataset-platform-web:enterprise'
                DP_SCAN_IMAGE = 'dataset-platform-scanner:enterprise'
            }
            $pinsToRefresh = @{}
            $previousProcessValues = @{}
            try {
                if ($Build) {
                    foreach ($name in $imageTags.Keys) {
                        $processValue = [Environment]::GetEnvironmentVariable($name, 'Process')
                        $line = [regex]::Match($originalEnv, "(?m)^$([regex]::Escape($name))=([^\r\n]*)")
                        $configured = if ($processValue) { $processValue } elseif ($line.Success) { $line.Groups[1].Value.Trim('"', "'") } else { '' }
                        if ($configured -match '^(?:sha256:[a-f0-9]{64}|.+@sha256:[a-f0-9]{64})$') {
                            $previousProcessValues[$name] = $processValue
                            $pinsToRefresh[$name] = $imageTags[$name]
                            [Environment]::SetEnvironmentVariable($name, $imageTags[$name], 'Process')
                        }
                    }
                    Invoke-Compose @('build','api','web','clamav')
                    if ($pinsToRefresh.Count) {
                        $updatedLines = @([IO.File]::ReadAllLines($composeEnvPath))
                        if ([IO.File]::ReadAllText($composeEnvPath) -ne $originalEnv) {
                            throw 'Compose environment changed during build; refusing to replace it.'
                        }
                        foreach ($name in $pinsToRefresh.Keys) {
                            $builtId = & docker image inspect $pinsToRefresh[$name] --format '{{.Id}}'
                            if ($LASTEXITCODE -ne 0 -or $builtId -notmatch '^sha256:[a-f0-9]{64}$') {
                                throw "Cannot resolve newly built image for $name. Existing image pin was preserved."
                            }
                            $found = $false
                            for ($index = 0; $index -lt $updatedLines.Length; $index++) {
                                if ($updatedLines[$index] -match "^$name=") {
                                    $updatedLines[$index] = "$name=$builtId"
                                    $found = $true
                                }
                            }
                            if (-not $found) { $updatedLines += "$name=$builtId" }
                            [Environment]::SetEnvironmentVariable($name, $builtId, 'Process')
                        }
                        $tempEnvPath = Join-Path $workspace "deployment/compose.env.local.build-$PID.tmp"
                        try {
                            [IO.File]::WriteAllLines($tempEnvPath, [string[]]$updatedLines, [Text.UTF8Encoding]::new($false))
                            Move-Item -LiteralPath $tempEnvPath -Destination $composeEnvPath -Force
                        } finally {
                            if (Test-Path -LiteralPath $tempEnvPath) { Remove-Item -LiteralPath $tempEnvPath -Force }
                        }
                        Write-Host 'Local application image pin updated after a successful build.'
                    }
                }
                if ($BuildOnly) {
                    Write-Host 'Images built and local image pin refreshed; running services were not changed.'
                    break
                }
                & $python scripts/schema_preflight.py -- @composeArgs
                if ($LASTEXITCODE -ne 0) { throw 'Schema compatibility check stopped start. Use the reviewed maintenance procedure; no running services were replaced.' }
                if ($activeProfile -and $activeProfile.auto_model) {
                    try { & powershell -ExecutionPolicy Bypass -File scripts/start-lmstudio.ps1 }
                    catch { Write-Warning 'Model startup failed; inspect LM Studio separately.' }
                }
                Invoke-Compose @('up','-d','--no-build')
                Invoke-Compose @('ps')
                if ($activeProfile) { Write-Host "HTTPS entry: $($activeProfile.site_url)" }
            } finally {
                foreach ($name in $previousProcessValues.Keys) {
                    [Environment]::SetEnvironmentVariable($name, $previousProcessValues[$name], 'Process')
                }
            }
        }
        'status' {
            Invoke-Compose @('ps')
            if ($activeProfile) {
                $previousSettingsFile = [Environment]::GetEnvironmentVariable('DP_SETTINGS_FILE','Process')
                try {
                    $env:DP_SETTINGS_FILE = $activeProfile.host_env
                    $modelReport = & $python scripts/model_health.py
                    if ($LASTEXITCODE -eq 0) { $modelReport | ConvertFrom-Json | Format-Table service,status,model,seconds -AutoSize }
                } finally { [Environment]::SetEnvironmentVariable('DP_SETTINGS_FILE',$previousSettingsFile,'Process') }
            }
        }
        'stop' { Invoke-Compose @('stop') }
        'reload-app' {
            & $python scripts/schema_preflight.py -- @composeArgs
            if ($LASTEXITCODE -ne 0) { throw 'Schema compatibility check stopped reload. Use the reviewed maintenance procedure.' }
            if (-not $activeProfile) { throw 'reload-app requires an active fresh deployment.' }
            $appServices = @('api','worker','vision-worker','beat')
            $definedServices = @(& docker @composeArgs config --services)
            if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect selected deployment services.' }
            if ($definedServices -contains 'mail-worker') { $appServices += 'mail-worker' }
            Invoke-Compose (@('up','-d','--no-deps','--no-build','--force-recreate') + $appServices)
        }
        'create-admin' {
            if (-not $activeProfile -or -not $Username) { throw 'Use -Username with an active fresh deployment.' }
            & $python scripts/bootstrap_account.py --env-file $SourceEnv --username $Username --name $Username --admin
            if ($LASTEXITCODE -ne 0) { throw 'Administrator provisioning failed.' }
        }
        'backup' {
            if (-not $Bundle) { throw 'Specify -Bundle; existing backup files are never overwritten.' }
            if (Test-Path -LiteralPath $Bundle) { throw 'Backup destination already exists; choose a new file.' }
            $backupKey = if ($activeProfile) { Join-Path $activeProfile.secrets_dir 'backup_key' } else { Join-Path $workspace 'deployment/secrets/backup_key' }
            if (-not (Test-Path -LiteralPath $backupKey)) { throw 'The selected instance backup key is missing. No services were stopped.' }
            # Capture exact existing containers. `compose up` would reconcile desired image
            # pins/dependencies and can accidentally deploy a build or run migrations.
            $appServices = @('web','api','worker','vision-worker','beat')
            $definedServices = @(& docker @composeArgs config --services)
            if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect selected deployment services.' }
            if ($definedServices -contains 'mail-worker') { $appServices += 'mail-worker' }
            $runningIds = @(& docker @composeArgs ps --status running -q @appServices)
            if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect running application containers.' }
            foreach ($containerId in $runningIds) { if ($containerId -notmatch '^[a-f0-9]{12,64}$') { throw 'Invalid container identity.' } }
            & powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop-dev.ps1
            if ($LASTEXITCODE -ne 0) { throw 'Local writer shutdown failed.' }
            try {
                Invoke-Compose @('stop','web','api')
                if ($definedServices -contains 'mail-worker') { Invoke-Compose @('stop','mail-worker') }
                # 无活动任务或未完成上传时才生成快照，拒绝不一致备份。
                & $python scripts/platform_data.py inventory --source-env $SourceEnv
                if ($LASTEXITCODE -ne 0) { throw 'Drain pending jobs and complete/abort uploads, then retry backup.' }
                Invoke-Compose @('stop','beat','worker','vision-worker')
                & $python scripts/platform_data.py backup --source-env $SourceEnv --bundle $Bundle --key-file $backupKey --quiesced
                if ($LASTEXITCODE -ne 0) { throw 'Backup failed; original data remains intact.' }
            } finally {
                if ($runningIds.Count) {
                    & docker start @runningIds
                    if ($LASTEXITCODE -ne 0) { throw 'Backup finished but original containers could not all restart. Inspect their state before another deployment.' }
                }
            }
        }
    }
} finally {
    if ($operationLock) { $operationLock.Dispose() }
    Pop-Location
}
