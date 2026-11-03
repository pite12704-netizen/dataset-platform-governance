"""本地账号、独立注册策略、真实邮件状态和密码恢复安全边界。"""
import re
import smtplib
import ssl

from sqlalchemy import func, select

from app.auth import challenge, deliver_mail
from app.models import Challenge, MailOutbox, User
from test_imports import create_import, png
from test_lifecycle import create_dataset


def test_private_local_account_without_email_requires_initial_password_change(app, client, actors):
    app.state.settings.deployment = 'private'
    payload = {'username': 'Local.Reader', 'name': '本地用户', 'password': 'Initial-local-password!'}
    assert client.post('/api/v1/admin/users/local', headers=actors['reader']['headers'], json=payload).status_code == 403
    response = client.post('/api/v1/admin/users/local', headers=actors['admin']['headers'], json=payload)
    assert response.status_code == 201 and response.json()['email'] is None, response.text
    assert response.json()['username'] == 'local.reader' and response.json()['must_change_password'] is True
    login = client.post('/api/v1/auth/login', json={'account': 'LOCAL.READER', 'password': payload['password']})
    assert login.status_code == 200 and login.json()['password_change_required'] is True
    headers = {'Authorization': 'Bearer ' + login.json()['access_token']}; client.cookies.clear()
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 200
    assert client.get('/api/v1/spaces', headers=headers).status_code == 403
    result = client.post('/api/v1/auth/change-password', headers=headers, json={
        'current_password': payload['password'], 'password': 'User-chosen-password!2026'})
    assert result.status_code == 200, result.text
    assert client.get('/api/v1/auth/me', headers=headers).status_code == 401
    login = client.post('/api/v1/auth/login', json={'account': 'local.reader', 'password': 'User-chosen-password!2026'})
    headers = {'Authorization': 'Bearer ' + login.json()['access_token']}; client.cookies.clear()
    assert login.json()['password_change_required'] is False
    assert client.get('/api/v1/spaces', headers=headers).status_code == 200
    setup = client.post('/api/v1/auth/mfa/setup', headers=headers)
    assert setup.status_code == 200 and 'local.reader' in setup.json()['uri']


def test_registration_policy_is_independent_and_new_users_cannot_select_roles(app, client):
    cfg = app.state.settings; cfg.deployment = 'private'
    data = {'username': 'new-local', 'name': '新用户', 'password': 'Chosen-long-password!'}
    assert client.post('/api/v1/auth/register', json=data).status_code == 403
    cfg.registration_mode = 'open'; cfg.require_verification = False
    config = client.get('/api/v1/config').json()
    assert config['registration_enabled'] and not config['registration_email_required']
    response = client.post('/api/v1/auth/register', json=data)
    assert response.status_code == 201, response.text
    with app.state.db.Session() as db:
        user = db.scalar(select(User).where(User.username == 'new-local'))
        assert user.verified and not user.platform_admin and user.email is None
    assert client.post('/api/v1/auth/register', json={**data, 'username': 'another', 'platform_admin': True}).status_code == 422
    cfg.deployment = 'public'
    assert client.post('/api/v1/auth/register', json={**data, 'username': 'public-user'}).status_code == 422
    cfg.registration_mode = 'closed'
    assert not client.get('/api/v1/config').json()['registration_enabled']
    assert client.post('/api/v1/auth/register', json={**data, 'email': 'new@example.test'}).status_code == 403


def test_production_open_registration_cannot_advertise_unavailable_verification():
    import pytest
    from pydantic import ValidationError
    from test_enterprise_operations import production
    with pytest.raises(ValidationError, match='SMTP'):
        production(registration_mode='open', require_verification=True)
    local = production(registration_mode='open', require_verification=False)
    assert local.registration_enabled and not local.registration_email_required


def test_no_smtp_in_production_does_not_claim_delivery_or_exhaust_queue(app, client, actors):
    # Only switch policy on an explicitly isolated fixture; no production connection.
    cfg = app.state.settings; cfg.environment = 'production'; cfg.deployment = 'private'
    with app.state.db.Session() as db:
        challenge(db, db.get(User, actors['reader']['id']), 'reset', cfg); db.commit()
        deliver_mail(db, cfg)
        mail = db.scalar(select(MailOutbox))
        assert mail.sent_at is None and mail.attempts == 0
    config = client.get('/api/v1/config').json()
    assert config['mail_delivery_mode'] == 'disabled' and config['password_reset_available'] is False
    for email in ('reader@example.test', 'unknown@example.test'):
        assert client.post('/api/v1/auth/forgot-password', json={'email': email}).status_code == 503
    assert client.post('/api/v1/admin/users/invite', headers=actors['admin']['headers'],
        json={'email': 'invite@example.test', 'name': '未邀请'}).status_code == 503
    assert client.get('/api/v1/admin/mail/status', headers=actors['reader']['headers']).status_code == 403
    status = client.get('/api/v1/admin/mail/status', headers=actors['admin']['headers']).json()
    assert status['pending'] == 1 and status['submitted_to_smtp'] == 0


def test_smtp_submission_and_failure_are_distinct_and_tls_verified(app, client, actors, monkeypatch):
    cfg = app.state.settings; cfg.smtp_host = 'smtp.example.test'; cfg.mail_from = 'dataset@example.test'
    class SMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, context):
            assert context.verify_mode == ssl.CERT_REQUIRED
        def send_message(self, message):
            assert 'reset=' in message.get_content()
    monkeypatch.setattr(smtplib, 'SMTP', SMTP)
    response = client.post('/api/v1/auth/forgot-password', json={'email': 'reader@example.test'})
    assert response.status_code == 200 and '不代表' in response.json()['message']
    with app.state.db.Session() as db:
        mail = db.scalar(select(MailOutbox))
        assert mail.delivery_mode == 'smtp' and mail.sent_at is not None and 'reset=' not in mail.body
    class FailedSMTP(SMTP):
        def send_message(self, message):
            raise smtplib.SMTPRecipientsRefused({'synthetic': (550, b'synthetic rejection')})
    monkeypatch.setattr(smtplib, 'SMTP', FailedSMTP)
    client.post('/api/v1/auth/forgot-password', json={'email': 'owner@example.test'})
    with app.state.db.Session() as db:
        pending = db.scalar(select(MailOutbox).where(MailOutbox.sent_at.is_(None)))
        assert pending.last_error == 'SMTPRecipientsRefused' and pending.attempts == 1
    # Account existence remains hidden behind the same generic acknowledgement.
    assert client.post('/api/v1/auth/forgot-password', json={'email': 'unknown@example.test'}).json()['message'] == response.json()['message']
    class SSL(SMTP):
        def __init__(self, *args, **kwargs):
            assert kwargs['context'].verify_mode == ssl.CERT_REQUIRED
        def starttls(self, **kwargs):
            raise AssertionError('SSL 不应再次调用 STARTTLS')
    monkeypatch.setattr(smtplib, 'SMTP_SSL', SSL)
    cfg.smtp_ssl = True; cfg.smtp_port = 465
    assert client.post('/api/v1/auth/forgot-password', json={'email': 'admin@example.test'}).status_code == 200


def test_local_account_migration_preserves_old_login_and_refuses_lossy_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config
    from app.config import Settings
    from app.db import Database
    from app.auth import add_user
    from app.schemas import Register
    from sqlalchemy import text
    import pytest
    settings = Settings(_env_file=None, environment='test', secret_key='isolated-account-migration-0123456789012345',
                        database_url='sqlite:///' + str(tmp_path/'accounts.db'))
    cfg = Config('backend/alembic.ini'); cfg.attributes['settings'] = settings
    command.upgrade(cfg, 'b9e81a7302fd')
    database = Database(settings.database_url)
    try:
        with database.engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id,created_at,email,name,password_hash,active,verified,platform_admin,mfa_enabled,mfa_last_counter) VALUES (:id,:time,:email,:name,:hash,1,1,0,0,-1)"),
                {'id': 'a'*32, 'time': '2026-10-28 00:00:00', 'email': 'legacy@example.test', 'name': 'legacy', 'hash': 'existing-hash-is-preserved'})
        command.upgrade(cfg, 'head')
        with database.Session() as db:
            user = db.get(User, 'a'*32)
            assert user.email == 'legacy@example.test' and user.username is None and not user.must_change_password
            assert user.password_hash == 'existing-hash-is-preserved'
            add_user(db, Register(username='offline.user', name='本地', password='Local-migration-test-password!'), settings, verified=True)
            db.commit()
        with pytest.raises(RuntimeError, match='without email'):
            command.downgrade(cfg, 'b9e81a7302fd')
    finally:
        database.engine.dispose()


def test_admin_reset_revokes_sessions_credentials_and_other_reset_links(app, client, actors):
    reader = actors['reader']; _, version = create_dataset(client, reader,modality='image')
    ingest = create_import(client, reader, version, png())
    credential = client.post('/api/v1/credentials', headers=reader['headers'], json={'name': 'test', 'scopes': ['catalog:read']}).json()
    app.state.settings.task_mode = 'manual'
    client.post('/api/v1/auth/forgot-password', json={'email': 'reader@example.test'})
    client.post('/api/v1/auth/forgot-password', json={'email': 'reader@example.test'})
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count(Challenge.id)).where(Challenge.kind == 'reset')) == 1
        token = re.search(r'reset=([\w-]+)', db.scalar(select(MailOutbox)).body).group(1)
    payload = {'password': 'New-admin-set-password!', 'reason': '核验用户身份后恢复访问'}
    path = f'/api/v1/admin/users/{reader["id"]}/reset-password'
    assert client.post(path, headers=actors['owner']['headers'], json=payload).status_code == 403
    assert client.post(path, headers=actors['admin']['headers'], json=payload).status_code == 200
    assert client.get('/api/v1/auth/me', headers=reader['headers']).status_code == 401
    assert client.get('/api/v1/datasets', headers={'Authorization': 'Bearer ' + credential['token']}).status_code == 401
    assert client.get('/api/v1/imports/' + ingest['id'], headers=ingest['headers']).status_code == 401
    assert client.post('/api/v1/auth/reset-password', json={'token': token, 'password': 'Attacker-other-password!'}).status_code == 400
    login = client.post('/api/v1/auth/login', json={'account': 'reader@example.test', 'password': payload['password']})
    assert login.status_code == 200 and login.json()['password_change_required'] is True


def test_existing_email_user_can_add_username_and_membership_uses_account(client, actors):
    owner = actors['owner']
    payload = {'username': 'Owner.Local', 'current_password': 'A-long-test-password!'}
    assert client.put('/api/v1/auth/username', headers=owner['headers'], json={**payload, 'current_password': 'wrong'}).status_code == 400
    assert client.put('/api/v1/auth/username', headers=owner['headers'], json=payload).json()['username'] == 'owner.local'
    assert client.put('/api/v1/auth/username', headers=actors['reader']['headers'], json=payload).status_code == 409
    for account in ('OWNER.LOCAL', 'owner@example.test'):
        response = client.post('/api/v1/auth/login', json={'account': account, 'password': payload['current_password']})
        assert response.status_code == 200
        client.cookies.clear()
    org = client.post('/api/v1/spaces', headers=actors['reader']['headers'], json={'name': '账号成员组织'}).json()
    response = client.post('/api/v1/spaces/' + org['id'] + '/members', headers=actors['reader']['headers'],
        json={'email': 'owner.local', 'role': 'reviewer'})
    assert response.status_code == 200, response.text
