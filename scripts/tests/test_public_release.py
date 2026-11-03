import json,os,subprocess,sys
from pathlib import Path
from scripts import public_release as release,audit_repository as audit,audit_git_history as history

def repository(path):
    subprocess.run(['git','init','-q',str(path)],check=True)

def command(path,*args):
    return subprocess.run(['git','-C',str(path),*args],check=True,capture_output=True).stdout

def test_forced_environment_and_gitlink_are_rejected_without_values(tmp_path,monkeypatch):
    repository(tmp_path);monkeypatch.setattr(audit,'ROOT',tmp_path)
    (tmp_path/'.env').write_text('DP_SECRET_KEY=not-a-real-test-secret\n')
    command(tmp_path,'add','-f','.env')
    command(tmp_path,'update-index','--add','--cacheinfo','160000,'+'1'*40+',usermanage')
    report=audit.audit(staged=True)
    assert not report['passed'] and {f['reason'] for f in report['findings']}=={'environment_file','gitlink_or_symlink'}
    assert 'not-a-real-test-secret' not in json.dumps(report)

def test_deleted_secret_is_still_found_in_history(tmp_path,monkeypatch):
    repository(tmp_path);monkeypatch.setattr(audit,'ROOT',tmp_path)
    secret='ghp_'+'Y'*36;(tmp_path/'old.py').write_text('value="'+secret+'"\n')
    command(tmp_path,'add','old.py');command(tmp_path,'-c','user.name=Synthetic','-c','user.email=test@example.invalid','commit','-qm','first')
    command(tmp_path,'rm','old.py');command(tmp_path,'-c','user.name=Synthetic','-c','user.email=test@example.invalid','commit','-qm','remove')
    assert audit.audit()['passed']
    report=history.inspect(tmp_path)
    assert report['commits']==2 and not report['passed']
    assert any(f['reason']=='credential_pattern' for f in report['findings'])
    assert secret not in json.dumps(report)

def test_export_omits_private_material_and_detects_changed_snapshot(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();monkeypatch.setattr(release,'ROOT',source);monkeypatch.setattr(audit,'ROOT',source)
    required=['LICENSE','backend/app/main.py','frontend/package-lock.json',*release.OVERRIDES.values()]
    for name in required:
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}' if name.endswith('.json') else 'public text\n')
    for name in ['.env','var/accounts.json','usermanage/hidden.py','docs/resume-private.md','datasets/real.txt']:
        path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private data')
    destination=tmp_path/'candidate';report=release.export(destination)
    assert report['passed'] and not (destination/'.env').exists() and not (destination/'usermanage').exists()
    assert not (destination/'var').exists() and not (destination/'docs/resume-private.md').exists()
    assert release.inspect(destination,True)['passed']
    (destination/'backend/app/main.py').write_text('changed')
    assert not release.inspect(destination,True)['passed']
    import pytest
    with pytest.raises(RuntimeError):release.export(destination)

def test_sandbox_prepare_never_uses_supplied_business_environment():
    root=Path(__file__).resolve().parents[2];active=root/'var/run/active-deployment.json'
    before=active.read_bytes() if active.exists() else None
    env={**os.environ,'DP_DATABASE_URL':'mysql+pymysql://example:test@127.0.0.1:9/forbidden','DP_SETTINGS_FILE':'missing-business.env'}
    result=subprocess.run([sys.executable,'scripts/start_public_sandbox.py','--prepare-only'],cwd=root,env=env,capture_output=True,text=True,timeout=40)
    assert result.returncode==0,'Isolated sandbox preparation failed'
    report=json.loads(result.stdout.splitlines()[-1]);directory=Path(report['directory'])
    assert report['isolated'] and not report['production'] and not report['models_enabled']
    content=(directory/'settings.env').read_text(encoding='utf-8')
    assert 'forbidden' not in content and (directory/'metadata.db').exists()
    assert (active.read_bytes() if active.exists() else None)==before
