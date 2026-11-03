"""仅供 test_bulk_ingest_isolated.py 新建的 MySQL/MinIO 使用。"""
import hashlib
import os
import uuid

import boto3
import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, func, select
from sqlalchemy.engine import make_url

from app.auth import add_user
from app.config import Settings
from app.main import create_app
from app.models import Asset, ImportItem, Space, Upload
from app.schemas import Register
from clients.ingest.exporter import export
from clients.ingest.uploader import PlatformClient, upload as push
from test_imports import create_import, png, register
from test_lifecycle import create_dataset
from test_review_mysql import concurrent_posts


@pytest.fixture(scope='module')
def isolated(tmp_path_factory):
    url = os.environ.get('DP_INGEST_TEST_DATABASE_URL')
    if not url:
        pytest.skip('运行 scripts/test_bulk_ingest_isolated.py 新建隔离容器')
    parsed = make_url(url)
    assert parsed.database == 'dataset_platform_ingest_test' and parsed.username == 'dp_ingest_test'
    assert parsed.host == '127.0.0.1' and os.environ.get('DP_INGEST_ISOLATED_RUN')
    root = tmp_path_factory.mktemp('ingest-mysql')
    endpoint = os.environ['DP_INGEST_TEST_S3_ENDPOINT']
    assert endpoint.startswith('http://127.0.0.1:')
    access, secret = os.environ['DP_INGEST_TEST_S3_ACCESS'], os.environ['DP_INGEST_TEST_S3_SECRET']
    s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret)
    for bucket in ('source-images', 'target-images'):
        s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket='source-images', VersioningConfiguration={'Status': 'Enabled'})
    settings = Settings(_env_file=None, environment='test', deployment='public',
        secret_key='isolated-ingest-test-only-01234567890123456789', database_url=url,
        storage_backend='s3', s3_endpoint=endpoint, s3_bucket='target-images', s3_access_key=access,
        s3_secret_key=secret, task_mode='inline', scan_mode='development', require_admin_mfa=False,
        require_verification=False, mail_spool=root/'mail', rate_limit_per_minute=10000,
        login_limit_per_minute=10000, part_size_bytes=5 * 1024**2)
    # Functional scenarios share this fixture and deliberately create many organizations.
    settings.account_max_organizations=100;settings.account_quota_budget_bytes=2*1024**4
    cfg = Config('backend/alembic.ini'); cfg.attributes['settings'] = settings
    command.upgrade(cfg, 'head')
    app = create_app(settings)
    with TestClient(app) as client:
        email = uuid.uuid4().hex + '@example.test'
        with app.state.db.Session() as db:
            user = add_user(db, Register(email=email, name='isolated importer', password='Isolated-import-test-password!'), settings, verified=True)
            db.commit(); identifier = user.id
        response = client.post('/api/v1/auth/login', json={'email': email, 'password': 'Isolated-import-test-password!'})
        headers = {'Authorization': 'Bearer ' + response.json()['access_token']}; client.cookies.clear()
        owner = {'id': identifier, 'headers': headers, 'space_id': client.get('/api/v1/spaces', headers=headers).json()[0]['id']}
        yield app, client, owner, s3, root
    app.state.db.engine.dispose(); s3.close()


def test_mysql_concurrent_manifest_and_upload_initialization_reserve_once(isolated):
    app, client, owner, _, _ = isolated
    _, version = create_dataset(client,owner,modality='image'); raw = png(); batch = create_import(client, owner, version, raw)
    payload = {'items': [{'external_id': 'shared-1', 'filename': 'shared.png', 'size': len(raw),
                         'sha256': hashlib.sha256(raw).hexdigest()}]}
    replies = concurrent_posts(app, [(f'/api/v1/imports/{batch["id"]}/items', batch, payload)] * 2)
    assert [r.status_code for r in replies] == [200, 200], [r.text for r in replies]
    assert replies[0].json()['items'][0]['id'] == replies[1].json()['items'][0]['id']
    item_id = replies[0].json()['items'][0]['id']
    replies = concurrent_posts(app, [(f'/api/v1/imports/{batch["id"]}/items/{item_id}/upload', batch, {})] * 2)
    assert [r.status_code for r in replies] == [200, 200], [r.text for r in replies]
    assert replies[0].json()['id'] == replies[1].json()['id']
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count(ImportItem.id)).where(ImportItem.batch_id == batch['id'])) == 1
        assert db.get(Space, owner['space_id']).reserved_bytes == len(raw)
    client.post(f'/api/v1/imports/{batch["id"]}/cancel', headers=owner['headers'])
    from app.tasks import sweep_storage
    sweep_storage(app.state.db, app.state.storage)


def test_real_minio_versioned_snapshot_push_and_target_hash(isolated):
    app, client, owner, s3, root = isolated
    raw = png()
    first = s3.put_object(Bucket='source-images', Key='group/a.png', Body=raw)
    s3.put_object(Bucket='source-images', Key='group/a.png', Body=b'newer object must not be copied')
    table = Table('customer_images_' + uuid.uuid4().hex[:12], MetaData(),
        Column('id', Integer, primary_key=True), Column('image_url', String(500)), Column('version', String(200)))
    table.create(app.state.db.engine)
    with app.state.db.engine.begin() as conn:
        conn.execute(table.insert(), [{'id': 1, 'image_url': 'https://source.invalid/images/group/a.png?X-Amz-Signature=strip-me', 'version': first['VersionId']}])
    config = {'source_id': 'customer-a', 'database': {'table': table.name,
        'columns': {'id': 'id', 'url': 'image_url', 'version': 'version'}},
        'storage': {'bucket': 'source-images', 'url_prefix': 'https://source.invalid/images/'}}
    output = root / 'export'
    summary = export(config, output, app.state.db.engine, s3)
    assert summary['complete'] and summary['files'] == 1
    assert 'strip-me' not in (output/'manifest.jsonl').read_text()
    _, version = create_dataset(client,owner,modality='image'); batch = create_import(client, owner, version, raw)
    def handle(request):
        response = client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
    target = PlatformClient('http://127.0.0.1', batch['token'], transport=httpx.MockTransport(handle))
    try:
        assert push(output/'manifest.jsonl', batch['id'], target, wait_seconds=0, progress=lambda _: None)['status'] == 'completed'
    finally:
        target.close()
    with app.state.db.Session() as db:
        item = db.scalar(select(ImportItem).where(ImportItem.batch_id == batch['id']))
        asset = db.get(Asset, item.asset_id)
        assert b''.join(app.state.storage.chunks(asset.object_key)) == raw
        assert asset.sha256 == hashlib.sha256(raw).hexdigest()


def test_real_mysql_release_snapshots_attribute_sql_and_minio_delivery(isolated):
    """Reuse the same contracts on MySQL JSON/window queries and real S3 bytes."""
    from conftest import actors as actors_fixture
    from test_quality_delivery import test_curated_delivery_is_server_generated_and_authorized
    from test_quality_delivery import test_other_batch_edit_invalidates_whole_version_evaluation
    from test_audit_hardening import test_attribute_and_matches_same_person_and_subset_preserves_all_people
    from test_audit_hardening import test_truth_is_not_in_training_grant_preview_agent_or_package
    app,client,_,_,_=isolated
    actors=actors_fixture.__wrapped__(app,client)
    with pytest.MonkeyPatch.context() as patch:
        test_curated_delivery_is_server_generated_and_authorized(app,client,actors,patch)
    test_other_batch_edit_invalidates_whole_version_evaluation(app,client,actors)
    test_attribute_and_matches_same_person_and_subset_preserves_all_people(app,client,actors)
    test_truth_is_not_in_training_grant_preview_agent_or_package(app,client,actors)


def test_real_mysql_blind_permissions_persistent_previews_and_pinned_context(isolated):
    from test_independent_evaluation import test_blind_truth_never_enters_producer_space_and_requires_designated_approver
    from test_persistent_previews_metrics import test_private_persistent_thumbnail_survives_memory_cache_and_is_counted
    from test_agent_pinned_context import test_pinned_sources_outlive_recent_window_but_never_outlive_data_permission
    app,client,_,_,_=isolated
    actors={};suffix=uuid.uuid4().hex[:8]
    for name in ('owner','reader','stranger','admin'):
        email=name+'-'+suffix+'@example.test'
        with app.state.db.Session() as db:
            user=add_user(db,Register(email=email,name=name,password='Isolated-import-test-password!'),app.state.settings,verified=True,admin=name=='admin')
            db.commit();identifier=user.id
        token=client.post('/api/v1/auth/login',json={'email':email,'password':'Isolated-import-test-password!'}).json()['access_token'];client.cookies.clear()
        headers={'Authorization':'Bearer '+token}
        actors[name]={'id':identifier,'headers':headers,'email':email,'space_id':client.get('/api/v1/spaces',headers=headers).json()[0]['id']}
    test_blind_truth_never_enters_producer_space_and_requires_designated_approver(app,client,actors)
    with pytest.MonkeyPatch.context() as patch:
        test_private_persistent_thumbnail_survives_memory_cache_and_is_counted(app,client,actors,patch)
    test_pinned_sources_outlive_recent_window_but_never_outlive_data_permission(app,client,actors)


def test_real_mysql_dispatch_claim_and_account_org_budget(isolated):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from unittest.mock import Mock
    from app.dispatching import send
    from app.models import Job
    app,client,owner,_,_=isolated
    _,version=create_dataset(client,owner,modality='image')
    with app.state.db.Session() as db:
        job=Job(space_id=owner['space_id'],user_id=owner['id'],version_id=version['id'],kind='quality',idempotency_key='dispatch-concurrent')
        db.add(job);db.commit();identifier=job.id
    broker=Mock();gate=Barrier(8)
    def claim(_):
        gate.wait(timeout=10)
        return send(identifier,app.state.db,app.state.settings,broker)
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(claim,range(8)))
    assert sum(results)==1 and broker.send_task.call_count==1
    with app.state.db.Session() as db:
        row=db.get(Job,identifier);row.status='cancelled';db.commit()
    password='Isolated-budget-test-password!';email=uuid.uuid4().hex+'@example.test'
    with app.state.db.Session() as db:
        user=add_user(db,Register(email=email,name='capacity tester',password=password),app.state.settings,verified=True)
        db.commit();new_id=user.id
    token=client.post('/api/v1/auth/login',json={'email':email,'password':password}).json()['access_token'];client.cookies.clear()
    actor={'id':new_id,'headers':{'Authorization':'Bearer '+token}}
    cfg=app.state.settings;previous=cfg.account_max_organizations;cfg.account_max_organizations=1
    try:
        replies=concurrent_posts(app,[('/api/v1/spaces',actor,{'name':'parallel org'})]*4)
        assert sorted(r.status_code for r in replies)==[201,409,409,409]
    finally:cfg.account_max_organizations=previous
