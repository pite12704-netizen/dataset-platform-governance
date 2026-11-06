import io
import json
from datetime import datetime

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from app.config import Settings
from scripts.platform_data import canonical, decode_manifest, decrypt_backup, encrypted_copy, upgrade_manifest
from app.checks import inspect_asset
from types import SimpleNamespace


def production(**overrides):
    values = dict(environment='production', deployment='private', secret_key='A'*40,
        database_url='mysql+pymysql://business:strong-pass@mysql/platform',storage_backend='s3',
        s3_access_key='business-key',s3_secret_key='business-storage-key',task_mode='celery',scan_mode='clamav',
        public_url='https://platform.example',frontend_url='https://platform.example',cookie_secure=True,
        require_admin_mfa=True,require_verification=True)
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize('override',[
    {'database_url':'sqlite:///local.db'}, {'database_url':'mysql+pymysql://root:pass@mysql/platform'},
    {'storage_backend':'local'}, {'task_mode':'inline'}, {'scan_mode':'development'},
    {'cookie_secure':False}, {'require_admin_mfa':False}, {'s3_access_key':''}, {'part_size_bytes':16}])
def test_production_refuses_incomplete_business_dependencies(override):
    with pytest.raises(ValidationError):
        production(**override)


def test_production_secrets_loaded_from_files(tmp_path):
    secret=tmp_path/'key'
    secret.write_text('B'*40)
    settings=production(secret_key_file=secret)
    assert settings.secret_key=='B'*40


def test_backup_authentication_rejects_modified_ciphertext(tmp_path):
    raw=b'private customer metadata\n'*10000
    key=bytes(range(32))
    bundle=tmp_path/'backup.dpbackup'
    encrypted_copy(io.BytesIO(raw),bundle,key)
    destination=io.BytesIO()
    decrypt_backup(bundle,destination,key)
    assert destination.read()==raw
    contents=bytearray(bundle.read_bytes())
    contents[len(contents)//2]^=1
    bundle.write_bytes(contents)
    with pytest.raises(InvalidTag):
        decrypt_backup(bundle,io.BytesIO(),key)


def test_backup_json_content_cannot_become_datetime():
    timestamp=datetime(2026,10,1,2,3,4,123456)
    raw=canonical({'tables':{'versions':[{'created_at':timestamp,'card':{'__dp_datetime__':'user content'}}]}})
    result=decode_manifest(raw)
    assert result['tables']['versions'][0]['created_at']==timestamp
    assert result['tables']['versions'][0]['card']=={'__dp_datetime__':'user content'}


def test_v1_backup_gets_deterministic_review_batch_defaults():
    tables={'image_review_batches':[{'id':'batch','version_id':'version','provenance':{}}],
            'image_review_items':[{'id':'item','batch_id':'batch','status':'pending'}]}
    manifest={'schema_version':1,'tables':tables,
              'metadata_sha256':__import__('hashlib').sha256(canonical(tables)).hexdigest()}
    result=upgrade_manifest(manifest)
    assert result['schema_version']==13
    assert result['tables']['image_review_batches'][0]['status']=='draft'
    assert result['tables']['image_review_batches'][0]['submitted_by_id'] is None


def test_scan_capacity_exceeded_never_reports_safe():
    settings=production(malware_max_bytes=1024)
    asset=SimpleNamespace(filename='customer.jpg',size=1025)
    with pytest.raises(RuntimeError,match='保持隔离'):
        inspect_asset(asset,None,settings)


def test_restore_discards_temporary_review_leases_and_upgrades_v2():
    import hashlib
    for version in (2, 3):
        tables = {'image_review_items': [{'id': 'item', 'status': 'submitted', 'revision': 4,
            'claimed_by_id': 'old-user', 'claim_token': 'old-token', 'claim_expires_at': datetime(2099, 1, 1)}]}
        manifest = {'schema_version': version, 'tables': tables,
                    'metadata_sha256': hashlib.sha256(canonical(tables)).hexdigest()}
        upgraded = upgrade_manifest(manifest)
        row = upgraded['tables']['image_review_items'][0]
        assert upgraded['schema_version'] == 13 and row['revision'] == 4
        assert row['claimed_by_id'] is None and row['claim_token'] is None and row['claim_expires_at'] is None
        assert upgraded['metadata_sha256'] == hashlib.sha256(canonical(upgraded['tables'])).hexdigest()


@pytest.mark.parametrize('active,valid',[(0,True),(1,True),(2,False),('1',False)])
def test_legacy_reflected_mysql_boolean_is_normalized_only_for_declared_columns(active,valid):
    import hashlib
    tables={'users':[{'id':'legacy-user','active':active,'verified':0}]}
    manifest={'schema_version':3,'tables':tables,'metadata_sha256':hashlib.sha256(canonical(tables)).hexdigest()}
    if not valid:
        with pytest.raises(RuntimeError,match='legacy boolean'):upgrade_manifest(manifest)
    else:
        upgraded=upgrade_manifest(manifest)
        row=upgraded['tables']['users'][0]
        assert type(row['active']) is bool and row['active']==bool(active) and row['verified'] is False
