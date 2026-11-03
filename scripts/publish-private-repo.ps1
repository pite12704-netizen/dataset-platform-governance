param([string]$RepositoryName = 'dataset-platform', [string]$GhPath)
# 【学习标注】只发布审核过的 Git 文件；账号授权在 GitHub 官方页面完成。
$ErrorActionPreference = 'Stop'
if ($RepositoryName -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$') { throw 'Invalid repository name.' }
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $workspace
try {
    $python = Join-Path $workspace '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
    if (-not $GhPath) {
        $command = Get-Command gh -ErrorAction SilentlyContinue
        if ($command) { $GhPath = $command.Source }
        else {
            $portable = Join-Path $workspace 'var\tools\github-cli\bin\gh.exe'
            if (Test-Path -LiteralPath $portable) { $GhPath = $portable }
        }
    }
    if (-not $GhPath) { throw 'Install the official GitHub CLI before publishing.' }
    & $GhPath auth status --hostname github.com
    if ($LASTEXITCODE -ne 0) {
        throw 'GitHub authentication is required. Run gh auth login --hostname github.com --git-protocol https --web.'
    }
    & $python scripts/audit_repository.py --staged
    if ($LASTEXITCODE -ne 0) { throw 'Staged source audit failed. Nothing has been uploaded.' }
    $dirty = & git diff --name-only
    if ($dirty) { throw 'Unstaged modifications exist. Audit and stage the final files first.' }
    & git rev-parse --verify HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Commit the reviewed files before publishing.' }
    $owner = & $GhPath api user --jq '.login'
    if ($LASTEXITCODE -ne 0 -or -not $owner) { throw 'Cannot verify the authenticated GitHub owner.' }
    $slug = "$owner/$RepositoryName"
    $remote = & git remote get-url origin 2>$null
    if ($LASTEXITCODE -eq 0) {
        if ($remote -ne "https://github.com/$slug.git") { throw 'Existing origin differs from the intended private repository.' }
    } else {
        $existing = & $GhPath repo view $slug --json isPrivate 2>$null
        if ($LASTEXITCODE -eq 0) { throw 'This repository name already exists. Choose a new name to avoid overwriting unrelated work.' }
        & $GhPath repo create $slug --private --source $workspace --remote origin --description 'Enterprise image dataset governance and Agent platform'
        if ($LASTEXITCODE -ne 0) { throw 'Private repository creation failed.' }
    }
    $visibility = & $GhPath repo view $slug --json isPrivate | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or -not $visibility.isPrivate) { throw 'Private visibility could not be confirmed. Upload stopped.' }
    & $GhPath auth setup-git --hostname github.com
    if ($LASTEXITCODE -ne 0) { throw 'Git authentication setup failed.' }
    & git push --set-upstream origin main
    if ($LASTEXITCODE -ne 0) { throw 'Git push failed; no successful upload is claimed.' }
    & $GhPath repo view $slug --json url,isPrivate,defaultBranchRef
    if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the uploaded repository.' }
} finally { Pop-Location }
