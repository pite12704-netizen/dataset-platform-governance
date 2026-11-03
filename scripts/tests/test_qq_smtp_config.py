from pathlib import Path

import pytest

from scripts.configure_qq_smtp import configure


def test_qq_configuration_preserves_other_settings_and_never_stores_plaintext_in_env(tmp_path):
    env = tmp_path/'deployment/service.env.local'; env.parent.mkdir()
    env.write_text('DP_DEPLOYMENT=private\nDP_SMTP_PASSWORD=old-synthetic\nDP_SMTP_HOST=old\nDP_SMTP_HOST=duplicate\n')
    configure(tmp_path, 'TEST@qq.com', 'syntheticcodeonly')
    content = env.read_text()
    assert 'DP_DEPLOYMENT=private' in content and content.count('DP_SMTP_HOST=') == 1
    assert 'DP_SMTP_SSL=true' in content and 'DP_SMTP_STARTTLS=false' in content
    assert 'DP_SMTP_PASSWORD=' not in content and 'syntheticcodeonly' not in content
    assert (tmp_path/'deployment/secrets/smtp_password').read_text() == 'syntheticcodeonly'


@pytest.mark.parametrize('email,code', [('other@example.test','syntheticcodeonly'),('test@qq.com','bad\ncode')])
def test_invalid_config_does_not_write_files(tmp_path,email,code):
    with pytest.raises(ValueError):
        configure(tmp_path,email,code)
    assert not list(tmp_path.rglob('*'))


def test_active_fresh_profile_updates_only_its_own_configuration(tmp_path):
    import json
    legacy=tmp_path/'deployment/service.env.local';legacy.parent.mkdir();legacy.write_text('legacy-unchanged')
    active=tmp_path/'var/run/active-deployment.json';active.parent.mkdir(parents=True)
    directory=tmp_path/'fresh';directory.mkdir()
    for name in ('service.env.local','host.env.local'):
        (directory/name).write_text('DP_DEPLOYMENT=private\n')
    active.write_text(json.dumps({'kind':'fresh-private','service_env':str(directory/'service.env.local'),
        'host_env':str(directory/'host.env.local'),'secrets_dir':str(directory/'secrets')}))
    configure(tmp_path,'test@qq.com','syntheticcodeonly')
    assert legacy.read_text()=='legacy-unchanged'
    assert 'DP_SMTP_HOST=smtp.qq.com' in (directory/'service.env.local').read_text()
    assert str(directory/'secrets/smtp_password') in (directory/'host.env.local').read_text()
