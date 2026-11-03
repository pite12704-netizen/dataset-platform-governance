"""固定输入、跨存储事务失败、崩溃残留与配额回收的业务回归。"""
import io
import uuid
import zipfile
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from app.db import now
from app.models import Asset, Job, Space, Upload
from app.storage import S3Storage
from app.tasks import run_job, sweep_storage
from test_lifecycle import create_dataset, upload


def package(client, owner, version):
    response = client.post('/api/v1/jobs', headers=owner['headers'], json={
        'version_id': version['id'], 'kind': 'package', 'idempotency_key': uuid.uuid4().hex})
    assert response.status_code == 201, response.text
    return response.json()


def test_package_uses_frozen_input_and_excludes_later_quarantine(app, client, actors):
    owner = actors['owner']; _, version = create_dataset(client, owner)
    _, first = upload(client, owner, version, b'original', 'original.txt')
    app.state.settings.task_mode = 'manual'
    job = package(client, owner, version)
    _, later = upload(client, owner, version, b'unscanned later file', 'later.txt')
    with app.state.db.Session() as db:
        assert db.get(Asset, later['asset_id']).status == 'quarantine'
    run_job(job['id'], app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        row = db.get(Job, job['id'])
        assert row.status == 'completed' and row.result['files'] == 1
        content = b''.join(app.state.storage.chunks(f"packages/{row.id}/{row.result['artifact_id']}"))
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.namelist() == [first['asset_id'] + '/original.txt']
        assert archive.read(archive.namelist()[0]) == b'original'


def test_input_rejected_after_enqueue_cannot_produce_package(app, client, actors):
    owner = actors['owner']; _, version = create_dataset(client, owner)
    _, asset = upload(client, owner, version)
    app.state.settings.task_mode = 'manual'
    job = package(client, owner, version)
    with app.state.db.Session() as db:
        db.get(Asset, asset['asset_id']).status = 'rejected'; db.commit()
    run_job(job['id'], app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        row = db.get(Job, job['id'])
        assert row.status == 'failed' and row.artifact_bytes == 0 and row.reserved_bytes == 0
        assert db.get(Space, owner['space_id']).reserved_bytes == 0


def test_crash_after_object_write_reclaims_previous_attempt(app, client, actors, monkeypatch):
    owner = actors['owner']; _, version = create_dataset(client, owner)
    upload(client, owner, version)
    app.state.settings.task_mode = 'manual'
    job = package(client, owner, version)
    original = app.state.storage.put_file
    def crash_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit('simulated worker death after upload before database commit')
    monkeypatch.setattr(app.state.storage, 'put_file', crash_after_write)
    with pytest.raises(SystemExit):
        run_job(job['id'], app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        row = db.get(Job, job['id']); old_token = row.run_token
        row.lease_until = now() - timedelta(seconds=1); db.commit()
        assert b''.join(app.state.storage.chunks(f'packages/{row.id}/{old_token}'))
    monkeypatch.setattr(app.state.storage, 'put_file', original)
    run_job(job['id'], app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        row = db.get(Job, job['id'])
        assert row.status == 'completed' and row.attempts == 2 and row.run_token != old_token
        assert db.get(Space, owner['space_id']).reserved_bytes == 0
    assert [p.name for p in app.state.storage.path(f'packages/{job["id"]}').iterdir()] == [row.run_token]


def test_delete_failure_is_retryable_and_releases_cancelled_task_quota(app, client, actors, monkeypatch):
    owner = actors['owner']; _, version = create_dataset(client, owner)
    _, asset = upload(client, owner, version)
    app.state.settings.task_mode = 'manual'
    job = package(client, owner, version)
    original = app.state.storage.delete
    def unavailable(*args):
        raise ConnectionError('synthetic storage failure')
    monkeypatch.setattr(app.state.storage, 'delete', unavailable)
    assert client.delete(f"/api/v1/assets/{asset['asset_id']}", headers=owner['headers']).status_code == 500
    with app.state.db.Session() as db:
        assert db.get(Asset, asset['asset_id']).status == 'removing'
        assert db.get(Job, job['id']).status == 'cancelled'
        assert db.get(Space, owner['space_id']).reserved_bytes == 0
    monkeypatch.setattr(app.state.storage, 'delete', original)
    assert client.delete(f"/api/v1/assets/{asset['asset_id']}", headers=owner['headers']).status_code == 200
    with app.state.db.Session() as db:
        assert db.get(Asset, asset['asset_id']) is None
        assert db.get(Space, owner['space_id']).used_bytes == 0


def test_one_expiry_cleanup_failure_does_not_block_other_uploads(app, client, actors, monkeypatch):
    owner = actors['owner']; _, version = create_dataset(client, owner)
    sessions = []
    for _ in range(2):
        response = client.post(f"/api/v1/versions/{version['id']}/uploads", headers=owner['headers'], json={
            'filename': 'expired.txt', 'size': 8, 'sha256': '0' * 64, 'idempotency_key': uuid.uuid4().hex})
        assert response.status_code == 201
        sessions.append(response.json())
    with app.state.db.Session() as db:
        for row in sessions:
            db.get(Upload, row['id']).expires_at = now() - timedelta(seconds=1)
        db.commit()
    original = app.state.storage.abort
    with app.state.db.Session() as db:
        blocked_key = db.get(Upload, sessions[0]['id']).object_key
    def fail_key(key, identifier):
        if key == blocked_key:
            raise ConnectionError('test')
        return original(key, identifier)
    monkeypatch.setattr(app.state.storage, 'abort', fail_key)
    sweep_storage(app.state.db, app.state.storage)
    with app.state.db.Session() as db:
        assert db.get(Upload, sessions[0]['id']).status == 'aborting'
        assert db.get(Upload, sessions[1]['id']).status == 'expired'
        assert db.get(Space, owner['space_id']).reserved_bytes == 8
    monkeypatch.setattr(app.state.storage, 'abort', original)
    sweep_storage(app.state.db, app.state.storage)
    with app.state.db.Session() as db:
        assert db.get(Space, owner['space_id']).reserved_bytes == 0


@pytest.mark.parametrize('operation', ['abort', 'complete'])
def test_s3_multipart_operations_handle_lost_success_ack(operation):
    class Client:
        def abort_multipart_upload(self, **kwargs):
            raise ClientError({'Error': {'Code': 'NoSuchUpload'}}, 'AbortMultipartUpload')
        def complete_multipart_upload(self, **kwargs):
            raise ClientError({'Error': {'Code': 'NoSuchUpload'}}, 'CompleteMultipartUpload')
        def head_object(self, **kwargs):
            self.head = kwargs
    storage = S3Storage.__new__(S3Storage); storage.bucket = 'test'; storage.client = Client()
    if operation == 'abort':
        storage.abort('server-key', 'server-upload')
    else:
        storage.complete('server-key', 'server-upload', [])
        assert storage.client.head == {'Bucket': 'test', 'Key': 'server-key'}
