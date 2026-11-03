"""隔离来源快照→真实平台分片→扫描→发布阻断；不访问本机业务服务。"""
import hashlib
import io
import json
import uuid
import zipfile
from datetime import timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine, func, select, text

from app.db import now
from app.models import Asset, ImportBatch, ImportItem, Job, Space, Upload
from clients.ingest.exporter import export, object_key
from clients.ingest.uploader import IngestFailure, PlatformClient, upload as push
from test_lifecycle import create_dataset, upload


def png():
    out = io.BytesIO(); Image.new('RGB', (30, 40), 'blue').save(out, 'PNG'); return out.getvalue()


def create_import(client, owner, version, raw, count=1):
    data = {'version_id': version['id'], 'source_id': 'customer-a', 'expected_files': count,
            'expected_bytes': len(raw) * count, 'idempotency_key': uuid.uuid4().hex}
    response = client.post('/api/v1/imports', headers=owner['headers'], json=data)
    assert response.status_code == 201, response.text
    batch = response.json(); batch['headers'] = {'Authorization': 'Bearer ' + batch['token']}
    return batch


def register(client, batch, raw, external_id='1', digest=None):
    data = {'external_id': external_id, 'filename': external_id + '.png', 'size': len(raw),
            'sha256': digest or hashlib.sha256(raw).hexdigest(), 'source_group': 'group-a'}
    response = client.post('/api/v1/imports/' + batch['id'] + '/items', headers=batch['headers'], json={'items': [data]})
    assert response.status_code == 200, response.text
    return response.json()['items'][0], data


def transfer(client, batch, item, raw):
    response = client.post(f'/api/v1/imports/{batch["id"]}/items/{item["id"]}/upload', headers=batch['headers'])
    assert response.status_code == 200, response.text
    session = response.json()
    for n, offset in enumerate(range(0, len(raw), session['part_size']), 1):
        response = client.put(f'/api/v1/uploads/{session["id"]}/parts/{n}', headers=batch['headers'],
                              content=raw[offset:offset + session['part_size']])
        assert response.status_code == 200, response.text
    response = client.post(f'/api/v1/uploads/{session["id"]}/complete', headers=batch['headers'])
    assert response.status_code == 200, response.text
    return session, response.json()


def test_scoped_ingest_resume_publish_guard_rotate_and_cross_tenant(app, client, actors):
    owner = actors['owner']; _, version = create_dataset(client, owner,modality='image')
    raw = png(); batch = create_import(client, owner, version, raw)
    kit = client.get('/api/v1/imports/client-kit', headers=owner['headers'])
    assert kit.status_code == 200
    with zipfile.ZipFile(io.BytesIO(kit.content)) as archive:
        assert 'clients/ingest/__main__.py' in archive.namelist()
        assert 'clients/ingest/local_source.py' in archive.namelist()
        assert all(name.startswith('clients/') and '.env' not in name for name in archive.namelist())
    path = '/api/v1/imports/' + batch['id']
    assert 'token_hash' not in batch
    assert client.get(path, headers=actors['stranger']['headers']).status_code == 404
    assert client.post('/api/v1/jobs', headers=batch['headers'], json={
        'version_id': version['id'], 'kind': 'quality', 'idempotency_key': uuid.uuid4().hex}).status_code == 401
    assert client.post('/api/v1/versions/' + version['id'] + '/submit', headers=owner['headers']).status_code == 409
    item, payload = register(client, batch, raw)
    assert client.post(path + '/finalize', headers=batch['headers']).status_code == 409
    assert client.post(path + '/items', headers=batch['headers'], json={'items': [payload]}).json()['items'][0]['id'] == item['id']
    changed = {**payload, 'sha256': 'a' * 64}
    assert client.post(path + '/items', headers=batch['headers'], json={'items': [changed]}).status_code == 409
    session, result = transfer(client, batch, item, raw)
    assert client.post(f'/api/v1/imports/{batch["id"]}/items/{item["id"]}/upload', headers=batch['headers']).json()['id'] == session['id']
    # Scoped credential cannot download its uploaded picture or touch another upload.
    assert client.get('/api/v1/assets/' + result['asset_id'] + '/download', headers=batch['headers']).status_code == 401
    other, _ = upload(client, owner, version, b'other text', 'other.txt')
    assert client.get('/api/v1/uploads/' + other['id'], headers=batch['headers']).status_code == 404
    _, other_version = create_dataset(client, owner,modality='image')
    foreign_batch = create_import(client, owner, other_version, raw)
    assert client.get('/api/v1/uploads/' + session['id'], headers=foreign_batch['headers']).status_code == 401
    replacement = client.post(path + '/credential', headers=owner['headers'])
    assert replacement.status_code == 200
    assert client.get(path, headers=batch['headers']).status_code == 401
    batch['headers'] = {'Authorization': 'Bearer ' + replacement.json()['token']}
    response = client.post(path + '/finalize', headers=batch['headers'])
    assert response.status_code == 200 and response.json()['status'] == 'completed', response.text
    manifest_hash = response.json()['manifest_hash']
    assert len(manifest_hash) == 64
    assert client.post(path + '/finalize', headers=batch['headers']).json()['manifest_hash'] == manifest_hash
    assert client.post('/api/v1/versions/' + version['id'] + '/submit', headers=owner['headers']).status_code == 200
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count(ImportItem.id))) == 1
        assert db.scalar(select(func.count(Upload.id)).where(Upload.filename == '1.png')) == 1
        asset = db.get(Asset, result['asset_id'])
        assert b''.join(app.state.storage.chunks(asset.object_key)) == raw


def test_failed_scan_is_explicit_and_retry_does_not_reupload(app, client, actors, monkeypatch):
    owner = actors['owner']; _, version = create_dataset(client, owner,modality='image'); raw = png()
    batch = create_import(client, owner, version, raw); item, _ = register(client, batch, raw)
    import app.tasks as tasks
    original = tasks.inspect_asset
    def fail(*args, **kwargs):
        raise ConnectionError('synthetic scanner failure')
    monkeypatch.setattr(tasks, 'inspect_asset', fail)
    _, result = transfer(client, batch, item, raw)
    path = '/api/v1/imports/' + batch['id']
    response = client.post(path + '/finalize', headers=batch['headers'])
    assert response.json()['status'] == 'partial_failed', response.text
    assert client.get(path + '/items', headers=batch['headers']).json()['items'][0]['status'] == 'failed'
    assert client.post('/api/v1/versions/' + version['id'] + '/submit', headers=owner['headers']).status_code == 409
    monkeypatch.setattr(tasks, 'inspect_asset', original)
    response = client.post(path + '/items/' + item['id'] + '/retry-scan', headers=batch['headers'])
    assert response.status_code == 200, response.text
    assert client.get(path, headers=batch['headers']).json()['status'] == 'completed'
    with app.state.db.Session() as db:
        assert db.get(ImportItem, item['id']).asset_id == result['asset_id']


def test_corruption_cancel_and_expired_upload_quota(app, client, actors):
    owner = actors['owner']; _, version = create_dataset(client, owner,modality='image'); raw = png()
    batch = create_import(client, owner, version, raw)
    item, _ = register(client, batch, raw)
    path = f'/api/v1/imports/{batch["id"]}/items/{item["id"]}/upload'
    old = client.post(path, headers=batch['headers']).json()
    with app.state.db.Session() as db:
        db.get(Upload, old['id']).expires_at = now() - timedelta(seconds=1); db.commit()
    renewed = client.post(path, headers=batch['headers'])
    assert renewed.status_code == 200 and renewed.json()['id'] != old['id'], renewed.text
    with app.state.db.Session() as db:
        space = db.get(Space, owner['space_id'])
        assert space.reserved_bytes == len(raw)
    assert client.post('/api/v1/imports/' + batch['id'] + '/cancel', headers=owner['headers']).status_code == 200
    assert client.get('/api/v1/uploads/' + renewed.json()['id'], headers=batch['headers']).status_code == 401
    from app.tasks import sweep_storage
    sweep_storage(app.state.db, app.state.storage)
    with app.state.db.Session() as db:
        assert db.get(Space, owner['space_id']).reserved_bytes == 0
    bad = create_import(client, owner, version, raw); item, _ = register(client, bad, raw, digest='a' * 64)
    transfer(client, bad, item, raw)
    response = client.post('/api/v1/imports/' + bad['id'] + '/finalize', headers=bad['headers'])
    assert response.json()['status'] == 'partial_failed' and response.json()['counts']['failed'] == 1


class FakeS3:
    def __init__(self, objects):
        self.objects = objects; self.reads = 0
    def head_object(self, Bucket, Key, **kwargs):
        return {'ContentLength': len(self.objects[Key]), 'ETag': 'version-one'}
    def get_object(self, Bucket, Key, IfMatch, **kwargs):
        assert IfMatch == 'version-one'; self.reads += 1
        return {'Body': io.BytesIO(self.objects[Key])}


def test_customer_mysql_mapping_minio_snapshot_to_real_platform_and_resume(app, client, actors, tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    with engine.begin() as db:
        db.execute(text('CREATE TABLE images(id INTEGER PRIMARY KEY,image_url TEXT,source_group TEXT)'))
        db.execute(text('INSERT INTO images VALUES (1,:url,:group)'), {'url': 'https://customer.example/images/a.png?X-Amz-Signature=never-export-this', 'group': 'camera-1'})
    config = {'source_id': 'customer-a', 'database': {'table': 'images', 'columns': {'id': 'id', 'url': 'image_url', 'group': 'source_group'}},
              'storage': {'bucket': 'images', 'url_prefix': 'https://customer.example/images/'}}
    source = FakeS3({'a.png': png()}); root = tmp_path / 'export'
    summary = export(config, root, engine, source)
    assert summary['complete'] and summary['files'] == 1 and source.reads == 1
    assert export(config, root, engine, source) == summary and source.reads == 1
    assert 'never-export-this' not in (root / 'records.jsonl').read_text()
    _, version = create_dataset(client, actors['owner'],modality='image')
    batch = create_import(client, actors['owner'], version, png())
    disconnected = [False]
    def handle(request):
        # Simulate a process stopping after the first acknowledged part.
        response = client.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers), content=request.content)
        if request.method == 'PUT' and not disconnected[0]:
            disconnected[0] = True
            raise KeyboardInterrupt()
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
    target = PlatformClient('http://127.0.0.1', batch['token'], transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(KeyboardInterrupt):
            push(root / 'manifest.jsonl', batch['id'], target, wait_seconds=0, progress=lambda _: None)
        result = push(root / 'manifest.jsonl', batch['id'], target, wait_seconds=0, progress=lambda _: None)
        assert result['status'] == 'completed'
    finally:
        target.close(); engine.dispose()
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count(Upload.id))) == 1
        asset = db.scalar(select(Asset))
        assert asset.sha256 == hashlib.sha256(png()).hexdigest()


def test_url_mapping_rejects_other_hosts_and_ignores_signatures():
    storage = {'url_prefix': 'https://customer.example/images/'}
    assert object_key('https://customer.example/images/sub/a.png?secret=discard', storage) == 'sub/a.png'
    with pytest.raises(ValueError):
        object_key('https://other.example/images/a.png', storage)


def test_browser_owner_cookie_upload_does_not_require_scoped_token(app, client, actors):
    """网页用现有登录会话操作本人的批次，既不保存也不传批次令牌。"""
    owner=actors['owner'];_,version=create_dataset(client, owner,modality='image');raw=png()
    batch=create_import(client,owner,version,raw)
    login=client.post('/api/v1/auth/login',json={'email':'owner@example.test','password':'A-long-test-password!'})
    assert login.status_code==200
    batch['headers']={'X-Requested-With':'dataset-platform'}
    item,_=register(client,batch,raw)
    _,result=transfer(client,batch,item,raw)
    response=client.post(f'/api/v1/imports/{batch["id"]}/finalize',headers=batch['headers'])
    assert response.status_code==200 and response.json()['status']=='completed',response.text
    assert client.post('/api/v1/auth/logout',headers=batch['headers']).status_code==200
    assert client.get(f'/api/v1/imports/{batch["id"]}',headers=batch['headers']).status_code==401
    with app.state.db.Session() as db:
        assert db.get(Asset,result['asset_id']).sha256==hashlib.sha256(raw).hexdigest()


def test_ordinary_rate_limit_does_not_interrupt_valid_multipart_import(app,client,actors):
    """真实分片接入走独立预算，页面查询额度耗尽也能继续原上传。"""
    owner=actors['owner'];_,version=create_dataset(client, owner,modality='image');raw=png()
    batch=create_import(client,owner,version,raw)
    app.state.settings.rate_limit_per_minute=1;app.state.settings.transfer_limit_per_minute=100
    app.state.rate_limiter.buckets.clear()
    assert client.get('/api/v1/config',headers=owner['headers']).status_code==200
    assert client.get('/api/v1/config',headers=owner['headers']).status_code==429
    item,_=register(client,batch,raw)
    _,result=transfer(client,batch,item,raw)
    response=client.post(f'/api/v1/imports/{batch["id"]}/finalize',headers=batch['headers'])
    assert response.status_code==200 and response.json()['status']=='completed',response.text
    with app.state.db.Session() as db:assert db.get(Asset,result['asset_id']).status=='clean'


def test_local_directory_paginated_upload_disconnect_and_resume(app, client, actors, tmp_path):
    """103 张合成原图跨越清单分页；重复文件名、中断续传均保留独立资产。"""
    from clients.ingest.local_source import prepare_local
    root = tmp_path / 'local-images'
    raw = png()
    for index in range(103):
        folder = root / str(index)
        folder.mkdir(parents=True)
        (folder / 'same.png').write_bytes(raw)
    output = tmp_path / 'local-manifest'
    summary = prepare_local(root, output, 'customer-a', '合成测试图片', '测试许可', progress=lambda _: None)
    assert summary['complete'] and summary['files'] == 103
    _, version = create_dataset(client, actors['owner'],modality='image')
    batch = create_import(client, actors['owner'], version, raw, count=103)
    app.state.settings.part_size_bytes = 8 * 1024**2
    interrupted = False
    registrations = []
    part_requests = 0

    def handle(request):
        nonlocal interrupted, part_requests
        if request.method == 'POST' and request.url.path.endswith('/items'):
            registrations.append(len(json.loads(request.content)['items']))
        if request.method == 'PUT':
            part_requests += 1
        response = client.request(request.method, request.url.raw_path.decode(),
                                  headers=dict(request.headers), content=request.content)
        if request.method == 'PUT' and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    target = PlatformClient('http://127.0.0.1', batch['token'], transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(KeyboardInterrupt):
            push(output / 'manifest.jsonl', batch['id'], target, input_dir=root,
                 wait_seconds=0, progress=lambda _: None)
        result = push(output / 'manifest.jsonl', batch['id'], target, input_dir=root,
                      wait_seconds=0, progress=lambda _: None)
        assert result['status'] == 'completed' and result['counts']['clean'] == 103
        assert registrations == [100, 100, 3]
        assert part_requests == 103  # 首张已收到的分片没有重传。
        assert push(output / 'manifest.jsonl', batch['id'], target, input_dir=root,
                    wait_seconds=0, progress=lambda _: None)['status'] == 'completed'
    finally:
        target.close()
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count(Asset.id))) == 103
        assert db.scalar(select(func.count(Upload.id))) == 103
        assert db.scalar(select(func.count(ImportItem.id))) == 103
        assert db.get(Space, actors['owner']['space_id']).reserved_bytes == 0
        assert db.get(Space, actors['owner']['space_id']).used_bytes == len(raw) * 103


def test_revocation_during_part_receiving_blocks_persistence(app, client, actors):
    _, version = create_dataset(client, actors['owner'],modality='image'); raw = png()
    batch = create_import(client, actors['owner'], version, raw); item, _ = register(client, batch, raw)
    session = client.post(f'/api/v1/imports/{batch["id"]}/items/{item["id"]}/upload', headers=batch['headers']).json()
    def body():
        yield raw[:8]
        with app.state.db.Session() as db:
            db.get(ImportBatch, batch['id']).revoked = True; db.commit()
        yield raw[8:16]
    response = client.put(f'/api/v1/uploads/{session["id"]}/parts/1', headers=batch['headers'], content=body())
    assert response.status_code == 401, response.text
    with app.state.db.Session() as db:
        assert db.get(Upload, session['id']).parts == {}


def test_vision_shards_keep_one_version_and_do_not_reschedule(app, client, actors):
    from test_image_reviews import setup_images
    owner, _, version, images, _ = setup_images(app, client, actors, 3)
    cfg = app.state.settings
    cfg.vision_enabled = True; cfg.model_url = 'http://127.0.0.1:1234/v1'; cfg.model_name = 'synthetic'
    cfg.vision_max_images = 2; cfg.task_mode = 'manual'
    payload = {'version_id': version['id'], 'kind': 'vision', 'idempotency_key': uuid.uuid4().hex}
    response = client.post('/api/v1/jobs/vision-batches', headers=owner['headers'], json=payload)
    assert response.status_code == 201 and len(response.json()['job_ids']) == 2, response.text
    with app.state.db.Session() as db:
        jobs = db.scalars(select(Job).where(Job.kind == 'vision')).all()
        inputs = [a['id'] for j in jobs for a in j.parameters['input_assets']]
        assert set(inputs) == set(images) and len(inputs) == len(set(inputs))
        assert {j.version_id for j in jobs} == {version['id']}
    assert client.post('/api/v1/jobs/vision-batches', headers=owner['headers'], json=payload).json()['job_ids'] == []
    assert client.post('/api/v1/versions/' + version['id'] + '/submit', headers=owner['headers']).status_code == 409
    from app.models import ImageReviewBatch, ImageReviewImage
    with app.state.db.Session() as db:
        first = ImageReviewBatch(version_id=version['id'], creator_id=owner['id'], status='completed', provenance={'origin': 'synthetic'})
        db.add(first); db.flush()
        for identifier in images[:2]:
            db.add(ImageReviewImage(batch_id=first.id, asset_id=identifier, width=64, height=80, status='approved'))
        db.commit()
    response = client.post('/api/v1/versions/' + version['id'] + '/submit', headers=owner['headers'])
    assert response.status_code == 409 and '视觉分片' in response.json()['message'], response.text


def test_restore_revokes_import_credentials():
    from scripts.platform_data import canonical, upgrade_manifest
    tables = {'import_batches': [{'id': 'batch', 'revoked': False}], 'import_items': []}
    manifest = {'schema_version': 5, 'tables': tables, 'metadata_sha256': hashlib.sha256(canonical(tables)).hexdigest()}
    assert upgrade_manifest(manifest)['tables']['import_batches'][0]['revoked'] is True


def test_import_migration_on_isolated_empty_database(tmp_path):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.config import Settings
    from app.db import Base
    url = 'sqlite:///' + str(tmp_path / 'migration.db')
    cfg = Config('backend/alembic.ini')
    cfg.attributes['settings'] = Settings(_env_file=None, environment='test', database_url=url,
        secret_key='isolated-migration-only-01234567890123456789')
    command.upgrade(cfg, 'head')
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.downgrade(cfg, 'a6c9e341d508')
        from app.db import Database
        from scripts.platform_data import snapshot, upgrade_manifest
        database = Database(url)
        try:
            previous = snapshot(database, legacy_schema=True)
            assert previous['schema_version'] == 4 and 'import_batches' not in previous['tables']
            assert upgrade_manifest(previous)['tables']['import_items'] == []
        finally:
            database.engine.dispose()
        command.upgrade(cfg, 'head')
    finally:
        engine.dispose()
