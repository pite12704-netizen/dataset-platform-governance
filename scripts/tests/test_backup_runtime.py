"""Execute the real backup branch with mocked commands; never touch Docker/services."""
import json
import subprocess
import shutil
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('fail_backup',[False,True])
@pytest.mark.skipif(shutil.which('powershell') is None,reason='Windows PowerShell runtime contract')
def test_backup_uses_instance_key_and_restarts_only_original_containers(tmp_path,fail_backup):
    source=ROOT/'scripts/enterprise.ps1'
    harness=tmp_path/'harness.ps1'
    harness.write_text(r'''
param([string]$Source,[string]$Sandbox,[int]$FailBackup)
$ErrorActionPreference='Stop'
$script:events=[System.Collections.Generic.List[object]]::new()
function docker {
  $script:events.Add(@{command='docker';arguments=@($args)})
  $global:LASTEXITCODE=0
  if ($args -contains 'ps') { ('a'*64);('b'*64) }
}
function powershell { $global:LASTEXITCODE=0 }
function Invoke-Compose([string[]]$Arguments) { $script:events.Add(@{command='compose';arguments=@($Arguments)}) }
function MockPython {
  $script:events.Add(@{command='python';arguments=@($args)})
  $global:LASTEXITCODE=if (($args -contains 'backup') -and $FailBackup) { 2 } else { 0 }
}
$python='MockPython';$workspace=$Sandbox;$SourceEnv='isolated-config-path'
$activeProfile=@{secrets_dir=$Sandbox};$composeArgs=@('compose','-p','isolated-fake-instance')
$Bundle=Join-Path $Sandbox 'backup.bin'
[IO.File]::WriteAllText((Join-Path $Sandbox 'backup_key'),'synthetic-unusable-key')
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'PowerShell parse failed' }
$switches=$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.SwitchStatementAst]},$true)
$branch=($switches.Clauses | Where-Object {$_.Item1.Value -eq 'backup'} | Select-Object -First 1).Item2.Extent.Text
if (-not $branch) { throw 'Backup branch not found' }
$body=$branch.Substring(1,$branch.Length-2)
$failed=$false
try { & ([scriptblock]::Create($body)) | Out-Null } catch { $failed=$true }
@{failed=$failed;events=@($script:events.ToArray())} | ConvertTo-Json -Depth 8 -Compress
''',encoding='utf-8')
    result=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(harness),
        '-Source',str(source),'-Sandbox',str(tmp_path),'-FailBackup',str(int(fail_backup))],
        capture_output=True,text=True,check=True,timeout=30)
    data=json.loads(result.stdout.strip())
    assert data['failed']==fail_backup
    events=data['events']
    backup=next(e for e in events if e['command']=='python' and 'backup' in e['arguments'])
    key_arg=backup['arguments'][backup['arguments'].index('--key-file')+1]
    assert Path(key_arg)==tmp_path/'backup_key'
    assert not any(e['command']=='compose' and 'up' in e['arguments'] for e in events)
    restart=next(e for e in events if e['command']=='docker' and 'start' in e['arguments'])
    assert restart['arguments']==['start','a'*64,'b'*64]
