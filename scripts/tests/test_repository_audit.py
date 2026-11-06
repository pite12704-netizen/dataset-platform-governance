import json
import subprocess

from scripts import audit_repository as audit


def test_credential_findings_never_include_secret_values():
    secret = 'sensitive-' + 'local-value-0123456789'
    report = audit.findings_for('app.py', 'value = "' + secret + '"', {secret})
    assert report == [{'path': 'app.py', 'line': 1, 'reason': 'matches_local_credential'}]
    assert secret not in json.dumps(report)
    token = 'ghp_' + 'A' * 36
    assert audit.findings_for('app.py', token, set())[0]['reason'] == 'credential_pattern'


def test_secret_file_paths_and_usernames_are_not_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    directory = tmp_path / 'deployment' / 'secrets'
    directory.mkdir(parents=True)
    (directory / 'minio_root_user').write_text('non-secret-username')
    (directory / 'app_key').write_text('real-' + 'test-secret-0123456789')
    (tmp_path / '.env').write_text('DP_SECRET_KEY_FILE=/run/secrets/app_key\n')
    values = audit.local_secrets()
    assert '/run/secrets/app_key' not in values
    assert 'non-secret-username' not in values
    assert 'real-' + 'test-secret-0123456789' in values


def test_staged_audit_catches_credential_removed_only_from_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    # FAT32 不记录文件所有者；仅显式信任本次刚创建的测试仓库，不修改全局配置。
    monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0','safe.directory')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0',tmp_path.as_posix())
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    secret = 'ghp_' + 'B' * 36
    source = tmp_path / 'app.py'
    source.write_text('token = "' + secret + '"\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'app.py'], check=True)
    source.write_text('token = None\n')
    assert audit.audit()['passed']
    report = audit.audit(staged=True)
    assert not report['passed']
    assert report['findings'][0]['reason'] == 'credential_pattern'
    assert secret not in json.dumps(report)


def test_active_retrieval_key_files_outside_old_secret_directory_are_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'ROOT', tmp_path)
    runtime = tmp_path / 'var/run'
    runtime.mkdir(parents=True)
    protected = tmp_path / 'external-service-config'
    protected.mkdir()
    value = 'synthetic-' + 'retrieval-service-key-0123456789'
    keyfile = protected / 'encoder-auth'
    keyfile.write_text(value, encoding='utf-8')
    host = protected / 'host.env.local'
    host.write_text('DP_RETRIEVAL_ENCODER_KEY_FILE='+str(keyfile)+'\n', encoding='utf-8')
    (runtime / 'active-deployment.json').write_text(json.dumps({'host_env': str(host)}), encoding='utf-8')
    values = audit.local_secrets()
    assert value in values and str(keyfile) not in values
    findings = audit.findings_for('candidate.py', value, values)
    assert findings and value not in json.dumps(findings)
