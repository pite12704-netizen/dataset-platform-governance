"""Regressions for the enterprise audit; isolated app fixture only."""
import hashlib
import io
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app.checks import inspect_asset
from app.db import now
from app.dispatching import send
from app.models import Job
from test_lifecycle import create_dataset


@pytest.mark.parametrize('format,extension', [('JPEG','jpg'),('PNG','png'),('WEBP','webp')])
def test_full_decode_preserves_valid_original_bytes(app,format,extension):
    image=io.BytesIO();Image.new('RGB',(50,40),'navy').save(image,format=format)
    data=image.getvalue();key='assets/valid-'+extension
    app.state.storage.put_bytes(key,data)
    asset=SimpleNamespace(filename='image.'+extension,size=len(data),sha256=hashlib.sha256(data).hexdigest(),object_key=key)
    result=inspect_asset(asset,app.state.storage,app.state.settings)
    assert result['status']=='clean' and result['structure']['method']=='image-full-decode-v2'
    assert b''.join(app.state.storage.chunks(key))==data


def test_truncated_jpeg_never_passes_full_check(app):
    image=io.BytesIO();Image.new('RGB',(80,80),'navy').save(image,format='JPEG')
    raw=image.getvalue()[:-10];key='assets/broken'
    app.state.storage.put_bytes(key,raw)
    asset=SimpleNamespace(filename='broken.jpg',size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),object_key=key)
    result=inspect_asset(asset,app.state.storage,app.state.settings)
    assert result['status']=='rejected' and result['structure']['status']=='failed'


def test_organizations_follow_configured_quota_and_account_budget(app,client,actors):
    cfg=app.state.settings;cfg.default_quota_bytes=1024;cfg.account_max_organizations=2
    cfg.account_quota_budget_bytes=10*1024**3+2048
    headers=actors['owner']['headers']
    for name in ('one','two'):
        response=client.post('/api/v1/spaces',headers=headers,json={'name':name})
        assert response.status_code==201 and response.json()['quota_bytes']==1024
    assert client.post('/api/v1/spaces',headers=headers,json={'name':'third'}).status_code==409


def test_dispatch_lease_bounds_repeated_sends_and_recovers(app,client,actors):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        job=Job(user_id=owner['id'],space_id=owner['space_id'],version_id=version['id'],kind='quality',idempotency_key='dispatch-one')
        db.add(job);db.commit();identifier=job.id
    broker=Mock()
    assert send(identifier,app.state.db,app.state.settings,broker)
    assert not send(identifier,app.state.db,app.state.settings,broker)
    assert broker.send_task.call_count==1
    with app.state.db.Session() as db:
        row=db.get(Job,identifier);row.next_dispatch_at=now()-timedelta(seconds=1);db.commit()
    assert send(identifier,app.state.db,app.state.settings,broker)
    assert broker.send_task.call_count==2
    with app.state.db.Session() as db:
        row=db.get(Job,identifier);row.status='completed';row.next_dispatch_at=None;db.commit()
    assert not send(identifier,app.state.db,app.state.settings,broker)


def test_broker_failure_waits_for_retry_deadline(app,client,actors):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        job=Job(user_id=owner['id'],space_id=owner['space_id'],version_id=version['id'],kind='quality',idempotency_key='broker-fails')
        db.add(job);db.commit();identifier=job.id
    broker=Mock();broker.send_task.side_effect=OSError('synthetic unavailable')
    assert not send(identifier,app.state.db,app.state.settings,broker)
    assert not send(identifier,app.state.db,app.state.settings,broker)
    assert broker.send_task.call_count==1


def test_mail_network_io_releases_sql_transaction_and_backs_off(app,monkeypatch):
    import smtplib
    from app.auth import deliver_mail
    from app.models import MailOutbox
    calls=[]
    cfg=app.state.settings;cfg.smtp_host='smtp.example.test'
    with app.state.db.Session() as db:
        mail=MailOutbox(recipient='synthetic@example.test',subject='synthetic',body='synthetic only')
        db.add(mail);db.commit();identifier=mail.id
        class SMTP:
            def __init__(self,*args,**kwargs):assert not db.in_transaction()
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def starttls(self,**kwargs):pass
            def send_message(self,message):
                assert not db.in_transaction();calls.append(message['Message-ID'])
                raise smtplib.SMTPException('synthetic unavailable')
        monkeypatch.setattr('app.auth.smtplib.SMTP',SMTP)
        assert deliver_mail(db,cfg)['failed']==1
        assert deliver_mail(db,cfg)['failed']==0
        row=db.get(MailOutbox,identifier);db.refresh(row)
        assert row.sent_at is None and row.attempts==1 and row.next_attempt_at>now()
        assert len(calls)==1 and row.delivery_token is None


def test_dedicated_mfa_key_rotation_preserves_legacy_and_can_retire(app,actors):
    from cryptography.fernet import Fernet, InvalidToken
    from app.security import cipher
    from app.models import User
    from scripts.rotate_mfa_keys import rotate
    cfg=app.state.settings;legacy=cipher(cfg).encrypt(b'synthetic-mfa-secret')
    with app.state.db.Session() as db:
        row=db.get(User,actors['owner']['id']);row.mfa_secret=legacy.decode();db.commit()
    cfg.mfa_encryption_keys=[Fernet.generate_key().decode()]
    assert cipher(cfg).decrypt(legacy)==b'synthetic-mfa-secret'
    assert rotate(app.state.db,cfg,False)['reencrypted']==0
    assert rotate(app.state.db,cfg,True)['reencrypted']==1
    cfg.mfa_allow_legacy_key=False
    with app.state.db.Session() as db:
        row=db.get(User,actors['owner']['id'])
        assert cipher(cfg).decrypt(row.mfa_secret.encode())==b'synthetic-mfa-secret'
    with pytest.raises(InvalidToken):cipher(cfg).decrypt(legacy)


def test_organization_ownership_requires_successor_confirmation(app,client,actors):
    owner,reader,stranger=[actors[name] for name in ('owner','reader','stranger')]
    space=client.post('/api/v1/spaces',headers=owner['headers'],json={'name':'handoff org'}).json()
    path='/api/v1/spaces/'+space['id']
    assert client.post(path+'/members',headers=owner['headers'],json={'email':'reader@example.test','role':'admin','active':True}).status_code==200
    request=client.post(path+'/ownership-transfers',headers=owner['headers'],json={
        'target_user_id':reader['id'],'reason':'synthetic ownership handoff','current_password':'A-long-test-password!'})
    assert request.status_code==201
    target=path+'/ownership-transfers/'+request.json()['id']+'/accept'
    credentials={'current_password':'A-long-test-password!'}
    assert client.post(target,headers=stranger['headers'],json=credentials).status_code==404
    assert client.post(target,headers=owner['headers'],json=credentials).status_code==404
    assert client.post(target,headers=reader['headers'],json=credentials).status_code==200
    assert client.post(target,headers=reader['headers'],json=credentials).status_code==409
    from app.models import Space
    with app.state.db.Session() as db:assert db.get(Space,space['id']).owner_id==reader['id']
    assert client.post(path+'/members',headers=reader['headers'],json={'email':'owner@example.test','role':'member','active':False}).status_code==200


def test_diagnostic_locations_never_log_exception_contents(caplog):
    import logging,json
    from app.diagnostics import report
    try:raise ValueError('sensitive-value-must-not-be-logged')
    except ValueError as exc:report(logging.getLogger('audit.test'),'synthetic',exc,job_id='synthetic-job')
    entry=json.loads(caplog.records[-1].message)
    assert entry['frames'] and entry['frames'][-1]['function']=='test_diagnostic_locations_never_log_exception_contents'
    assert 'sensitive-value-must-not-be-logged' not in caplog.text


def test_upload_remote_initialization_and_completion_release_sql(app,client,actors,monkeypatch):
    from test_lifecycle import upload
    sessions=[];factory=app.state.db.Session
    def tracked(*args,**kwargs):
        session=factory(*args,**kwargs);sessions.append(session);return session
    monkeypatch.setattr(app.state.db,'Session',tracked)
    start,complete=app.state.storage.initiate,app.state.storage.complete
    def initiate(key):
        assert all(not session.in_transaction() for session in sessions)
        return start(key)
    def finish(*args,**kwargs):
        assert all(not session.in_transaction() for session in sessions)
        return complete(*args,**kwargs)
    monkeypatch.setattr(app.state.storage,'initiate',initiate)
    monkeypatch.setattr(app.state.storage,'complete',finish)
    owner=actors['owner'];_,version=create_dataset(client,owner)
    _,result=upload(client,owner,version)
    assert result['asset_id']


def test_agent_release_gate_rejects_contract_only_stale_or_failed_evidence(app,tmp_path):
    import json
    from datetime import datetime,timezone
    from app.agent_release import REQUIRED_CASES,PROMPT_VERSION,validate
    cfg=app.state.settings;cfg.agent_model_revision='synthetic-revision';cfg.model_name='synthetic-model'
    path=tmp_path/'smoke.json';cfg.agent_release_evidence_file=path
    report={'mode':'live-synthetic-tool-probe','model':cfg.model_name,'model_revision':cfg.agent_model_revision,
        'prompt_version':PROMPT_VERSION,'generated_at':datetime.now(timezone.utc).isoformat(),
        'underlying_model_response_observed':True,'gate_passed':True,'business_data_read':False,
        'total':len(REQUIRED_CASES),'scenarios':[{'id':identifier,**dict.fromkeys(
            ['passed','workflow_passed','citation_passed','fact_assertions_passed','injection_safe'],True)} for identifier in REQUIRED_CASES]}
    def save():
        raw=json.dumps(report).encode();path.write_bytes(raw);cfg.agent_release_evidence_sha256=hashlib.sha256(raw).hexdigest()
    save();validate(cfg)
    report['mode']='scripted-contract';save()
    with pytest.raises(ValueError):validate(cfg)
    report['mode']='live-synthetic-tool-probe';report['gate_passed']=False;save()
    with pytest.raises(ValueError):validate(cfg)
    report['gate_passed']=True;report['generated_at']=(datetime.now(timezone.utc)-timedelta(days=15)).isoformat();save()
    with pytest.raises(ValueError):validate(cfg)


def test_platform_storage_growth_is_bounded_across_spaces(app,client,actors):
    from app.models import Space
    from sqlalchemy import select,func
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        used=db.scalar(select(func.coalesce(func.sum(Space.used_bytes+Space.reserved_bytes),0)))
    app.state.settings.platform_storage_budget_bytes=int(used)+4
    request={'filename':'capacity.txt','size':4,'sha256':'a'*64,'idempotency_key':'capacity-one'}
    assert client.post('/api/v1/versions/'+version['id']+'/uploads',headers=owner['headers'],json=request).status_code==201
    _,other=create_dataset(client,actors['reader'])
    assert client.post('/api/v1/versions/'+other['id']+'/uploads',headers=actors['reader']['headers'],json={**request,'idempotency_key':'capacity-two'}).status_code==409


def test_legacy_overbudget_account_can_reduce_but_not_expand_quota(app,client,actors):
    app.state.settings.account_quota_budget_bytes=1024**3
    path='/api/v1/admin/spaces/'+actors['owner']['space_id']+'/quota'
    assert client.put(path,headers=actors['admin']['headers'],json={'quota_bytes':8*1024**3}).status_code==200
    assert client.put(path,headers=actors['admin']['headers'],json={'quota_bytes':9*1024**3}).status_code==409
