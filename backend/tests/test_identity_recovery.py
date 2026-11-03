import re
from email import policy
from email.parser import BytesParser
import pyotp
from sqlalchemy import select
from app.models import User,Challenge,LoginSession

PASSWORD='A-long-test-password!'


def test_single_use_recovery_session_cannot_reach_business_and_revokes_credentials(app,client,actors):
    owner=actors['owner'];headers=owner['headers']
    service=client.post('/api/v1/credentials',headers=headers,json={'name':'before-recovery','scopes':['catalog:read'],'duration_days':1}).json()
    secret=client.post('/api/v1/auth/mfa/setup',headers=headers).json()['secret']
    response=client.post('/api/v1/auth/mfa/confirm',headers=headers,json={'code':pyotp.TOTP(secret).now()})
    assert response.status_code==200
    codes=response.json()['recovery_codes'];assert len(codes)==8
    assert 'mfa_recovery_hashes' not in client.get('/api/v1/auth/me',headers=headers).json()
    payload={'email':'owner@example.test','password':PASSWORD,'recovery_code':codes[0]}
    recovered=client.post('/api/v1/auth/login',json=payload)
    assert recovered.status_code==200 and recovered.json()['mfa_recovery_required']
    token={'Authorization':'Bearer '+recovered.json()['access_token']}
    assert client.post('/api/v1/auth/login',json=payload).status_code==401
    assert client.get('/api/v1/datasets',headers=token).status_code==403
    assert client.get('/api/v1/auth/me',headers=token).json()['recovery_only'] is True
    assert client.post('/api/v1/auth/mfa/recover',headers=token,json={'current_password':'wrong'}).status_code==401
    assert client.post('/api/v1/auth/mfa/recover',headers=token,json={'current_password':PASSWORD}).status_code==200
    assert client.get('/api/v1/auth/me',headers=headers).status_code==401
    assert client.get('/api/v1/datasets',headers={'Authorization':'Bearer '+service['token']}).status_code==401
    with app.state.db.Session() as db:
        user=db.get(User,owner['id']);assert not user.mfa_enabled and not user.mfa_secret and user.mfa_recovery_hashes==[]


def test_email_confirmation_is_account_bound_single_use_and_revokes_sessions(app,client,actors):
    owner=actors['owner'];path='/api/v1/auth/email/change'
    response=client.post(path,headers=owner['headers'],json={'email':'new@example.test','current_password':PASSWORD})
    assert response.status_code==200,response.text
    with app.state.db.Session() as db:assert db.get(User,owner['id']).email=='owner@example.test'
    token=None
    for file in app.state.settings.mail_spool.glob('*.eml'):
        message=BytesParser(policy=policy.default).parsebytes(file.read_bytes())
        if message['To']=='new@example.test':token=re.search(r'bind_email=([A-Za-z0-9_-]+)',message.get_content()).group(1)
    assert token
    data={'token':token,'current_password':PASSWORD}
    assert client.post('/api/v1/auth/email/confirm',headers=actors['stranger']['headers'],json=data).status_code==403
    assert client.post('/api/v1/auth/email/confirm',headers=owner['headers'],json=data).status_code==200
    assert client.get('/api/v1/auth/me',headers=owner['headers']).status_code==401
    logged=client.post('/api/v1/auth/login',json={'account':'new@example.test','password':PASSWORD})
    assert logged.status_code==200
    assert client.post('/api/v1/auth/email/confirm',headers={'Authorization':'Bearer '+logged.json()['access_token']},json=data).status_code==400
    with app.state.db.Session() as db:assert db.get(User,owner['id']).email=='new@example.test'


def test_disabled_production_mail_never_creates_email_change_challenge(app,client,actors):
    app.state.settings.environment='production'
    response=client.post('/api/v1/auth/email/change',headers=actors['owner']['headers'],json={'email':'new@example.test','current_password':PASSWORD})
    assert response.status_code==503
    with app.state.db.Session() as db:assert db.scalar(select(Challenge.id).where(Challenge.kind=='email_change')) is None
