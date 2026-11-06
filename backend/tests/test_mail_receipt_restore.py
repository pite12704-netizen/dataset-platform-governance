import hashlib
import io
import re
import zipfile
import copy
import pytest
from email import policy
from email.parser import BytesParser
from sqlalchemy import select
from app.models import MailOutbox
from app.db import now
from scripts.platform_data import snapshot,canonical,read_checked,encrypted_copy


def legacy_v8_manifest(app):
    from sqlalchemy import Boolean
    from app.db import Base
    manifest = snapshot(app.state.db)
    for name in ('control_locks', 'space_transfers','job_payloads','duplicate_pair_reviews'):
        manifest['tables'].pop(name)
    removed = {'jobs': ('dispatch_token', 'next_dispatch_at', 'dispatch_attempts'),
               'uploads': ('finalize_token', 'finalize_until'),
               'mail_outbox': ('delivery_token', 'lease_until', 'next_attempt_at')}
    for name, rows in manifest['tables'].items():
        for row in rows:
            for field in removed.get(name, ()):
                row.pop(field, None)
            for column in Base.metadata.tables[name].columns:
                if isinstance(column.type, Boolean) and row.get(column.name) is not None:
                    row[column.name] = int(row[column.name])
    manifest['schema_version'] = 8
    manifest['metadata_sha256'] = hashlib.sha256(canonical(manifest['tables'])).hexdigest()
    manifest['application_key_sha256'] = hashlib.sha256(app.state.settings.secret_key.encode()).hexdigest()
    return manifest


def test_legacy_v8_mysql_boolean_backup_restores_to_new_namespace(app, actors, tmp_path):
    from scripts.restore_exercise import exercise
    manifest = legacy_v8_manifest(app)
    assert type(manifest['tables']['users'][0]['active']) is int
    staged = io.BytesIO()
    with zipfile.ZipFile(staged, 'w') as archive:
        archive.writestr('manifest.json', canonical(manifest))
    key = tmp_path / 'backup_key'; key.write_text('ab' * 32)
    app_key = tmp_path / 'app_key'; app_key.write_text(app.state.settings.secret_key)
    bundle = tmp_path / 'legacy.enc'; staged.seek(0)
    encrypted_copy(staged, bundle, bytes.fromhex('ab' * 32))
    with pytest.raises(RuntimeError,match='bounded maintenance-backup budget'):
        exercise(bundle,key,app_key,max_metadata_bytes=1024)
    _, result = exercise(bundle, key, app_key,max_metadata_bytes=2*1024**2)
    assert result['schema_version'] == 13 and result['tables_verified'] == 48
    assert result['metadata_budget_bytes']==2*1024**2
    assert result['isolated'] and not result['business_services_accessed']


def test_legacy_boolean_checksum_is_verified_before_normalization(app, actors):
    from scripts.platform_data import upgrade_manifest
    manifest = legacy_v8_manifest(app)
    tampered = copy.deepcopy(manifest)
    tampered['tables']['users'][0]['active'] = 2
    with pytest.raises(RuntimeError, match='Metadata checksum failed'):
        upgrade_manifest(tampered)
    tampered['metadata_sha256'] = hashlib.sha256(canonical(tampered['tables'])).hexdigest()
    with pytest.raises(RuntimeError, match='Invalid legacy boolean'):
        upgrade_manifest(tampered)


def test_mail_probe_distinguishes_spool_smtp_acceptance_and_confirmed_receipt(app,client,actors):
    admin=actors['admin']
    assert client.post('/api/v1/admin/mail/probe',headers=actors['owner']['headers'],json={'email':'test@example.test'}).status_code==403
    response=client.post('/api/v1/admin/mail/probe',headers=admin['headers'],json={'email':'test@example.test'})
    assert response.status_code==200
    token=None
    for file in app.state.settings.mail_spool.glob('*.eml'):
        message=BytesParser(policy=policy.default).parsebytes(file.read_bytes())
        match=re.search(r'mail_probe=([A-Za-z0-9_-]+)',message.get_content())
        if match:token=match.group(1)
    assert token
    confirm='/api/v1/auth/mail/confirm-probe'
    assert client.post(confirm,headers=actors['owner']['headers'],json={'token':token}).status_code==403
    assert client.post(confirm,headers=admin['headers'],json={'token':token}).status_code==409
    with app.state.db.Session() as db:
        mail=db.scalar(select(MailOutbox).where(MailOutbox.subject.like('%收件探针%')))
        mail.delivery_mode='smtp';mail.sent_at=now();db.commit()
    assert client.post(confirm,headers=admin['headers'],json={'token':token}).status_code==200
    status=client.get('/api/v1/admin/mail/probe',headers=admin['headers']).json()
    assert status['smtp_accepted_at'] and status['received_confirmed_at']
    assert client.post(confirm,headers=admin['headers'],json={'token':token}).status_code==400


def test_encrypted_restore_exercise_uses_a_new_namespace_and_verifies_files(app,client,actors,tmp_path):
    from test_lifecycle import create_dataset,upload
    from scripts.restore_exercise import exercise
    _,version=create_dataset(client,actors['owner']);upload(client,actors['owner'],version)
    manifest=snapshot(app.state.db);manifest['application_key_sha256']=hashlib.sha256(app.state.settings.secret_key.encode()).hexdigest()
    staged=io.BytesIO()
    with zipfile.ZipFile(staged,'w') as archive:
        for index,item in enumerate(manifest['objects']):
            item['member']=f'objects/{index:08d}'
            raw=b''.join(app.state.storage.chunks(item['key']));archive.writestr(item['member'],raw)
            item['sha256']=read_checked([raw],item)
        archive.writestr('manifest.json',canonical(manifest))
    key=tmp_path/'backup_key';key.write_text('ab'*32)
    app_key=tmp_path/'app_key';app_key.write_text(app.state.settings.secret_key)
    bundle=tmp_path/'sample.enc';staged.seek(0);encrypted_copy(staged,bundle,bytes.fromhex('ab'*32))
    path,result=exercise(bundle,key,app_key)
    assert result['isolated'] and result['objects_verified']>=1 and result['schema_version']==13
    assert path.parent.name.startswith('restore-') and not result['business_services_accessed']
