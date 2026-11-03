"""隔离真实 MySQL + MinIO + ClamAV；必须显式指定集成环境文件。"""
import hashlib
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.auth import add_user
from app.config import Settings
from app.db import Base, Database
from app.main import create_app
from app.models import Asset, Job, Upload
from app.schemas import Register
from app.storage import make_storage

pytestmark = pytest.mark.skipif(not os.environ.get("DP_INTEGRATION_ENV"), reason="Real dependency profile required")


@pytest.fixture
def live():
    settings = Settings(_env_file=os.environ["DP_INTEGRATION_ENV"], environment="test", task_mode="inline",
        require_admin_mfa=False, require_verification=False, part_size_bytes=8 * 1024**2,
        rate_limit_per_minute=10000, login_limit_per_minute=10000)
    assert make_url(settings.database_url).database == "dataset_platform_integration"
    assert settings.s3_bucket == "datasets-integration" and settings.scan_mode == "clamav"
    db = Database(settings.database_url, settings)
    Base.metadata.drop_all(db.engine)
    app = create_app(settings, initialize=True)
    with TestClient(app) as client:
        actors = {}
        for name in ("owner", "reviewer", "reader", "outsider"):
            with app.state.db.Session() as session:
                user = add_user(session, Register(email=f"integration-{name}@example.test", name=name,
                    password="Integration-strong-password!"), settings, verified=True, admin=name == "reviewer")
                session.commit()
            response = client.post('/api/v1/auth/login', json={"email": user.email, "password": "Integration-strong-password!"})
            assert response.status_code == 200, response.text
            headers = {"Authorization": "Bearer " + response.json()["access_token"]}
            actors[name] = {"id": user.id, "headers": headers,
                "space_id": client.get('/api/v1/spaces', headers=headers).json()[0]['id']}
            client.cookies.clear()
        yield app, client, actors
    # 只清理本隔离测试库引用的对象，不枚举或清空业务桶。
    with app.state.db.Session() as session:
        storage = make_storage(settings)
        for row in session.scalars(select(Upload)):
            if row.status == "uploading":
                storage.abort(row.object_key, row.storage_upload_id)
        for asset in session.scalars(select(Asset)):
            storage.delete(asset.object_key)
        for job in session.scalars(select(Job)):
            if job.kind == "package" and job.result.get("artifact_id"):
                storage.delete(f"packages/{job.id}/{job.result['artifact_id']}")
    Base.metadata.drop_all(db.engine)
    db.engine.dispose()


def create(client, owner):
    response = client.post('/api/v1/datasets', headers=owner['headers'], json={"name": "真实存储集成验证",
        "space_id": owner['space_id'], "visibility": "public", "access_mode": "application"})
    assert response.status_code == 201, response.text
    dataset = response.json()
    response = client.post(f"/api/v1/datasets/{dataset['id']}/versions", headers=owner['headers'], json={
        "license": "internal-integration-fixture", "source": "Isolated fixture, not customer data"})
    assert response.status_code == 201, response.text
    return dataset, response.json()


def transfer(client, owner, version, data, digest=None):
    payload = {"filename": "samples.csv", "size": len(data), "sha256": digest or hashlib.sha256(data).hexdigest(),
               "idempotency_key": uuid.uuid4().hex, "preview_allowed": False}
    path = f"/api/v1/versions/{version['id']}/uploads"
    response = client.post(path, headers=owner['headers'], json=payload)
    assert response.status_code == 201, response.text
    upload = response.json()
    assert client.post(path, headers=owner['headers'], json=payload).json()['id'] == upload['id']
    for index, offset in enumerate(range(0,len(data),upload['part_size']),1):
        part = data[offset:offset+upload['part_size']]
        response = client.put(f"/api/v1/uploads/{upload['id']}/parts/{index}", headers=owner['headers'], content=part)
        assert response.status_code == 200, response.text
        restored = client.get(f"/api/v1/uploads/{upload['id']}",headers=owner['headers']).json()
        assert str(index) in restored['parts']
    path = f"/api/v1/uploads/{upload['id']}/complete"
    result = client.post(path, headers=owner['headers'])
    assert result.status_code == 200, result.text
    assert client.post(path, headers=owner['headers']).json()['asset_id'] == result.json()['asset_id']
    return result.json()['asset_id']


def test_real_multipart_clamav_publication_grant_range_revoke(live):
    app, client, actors = live
    owner, reviewer, reader = [actors[n] for n in ('owner','reviewer','reader')]
    _, version = create(client, owner)
    raw = b"x,y\n" + b"a,b\n" * (2300 * 1024)
    asset_id = transfer(client, owner, version, raw)
    with app.state.db.Session() as db:
        asset = db.get(Asset, asset_id)
        assert asset.status == 'clean' and asset.checks['malware_method'] == 'clamav'
        stored = b''.join(app.state.storage.chunks(asset.object_key))
        assert hashlib.sha256(stored).digest() == hashlib.sha256(raw).digest()
        key = asset.object_key
    import httpx
    anonymous = httpx.get(app.state.settings.s3_endpoint + '/datasets-integration/' + key, trust_env=False)
    assert anonymous.status_code == 403
    publication = client.post(f"/api/v1/versions/{version['id']}/submit",headers=owner['headers'])
    assert publication.status_code == 200, publication.text
    assert client.post(f"/api/v1/publications/{publication.json()['id']}/decision",headers=owner['headers'],json={'approve':True}).status_code == 403
    assert client.post(f"/api/v1/publications/{publication.json()['id']}/decision",headers=reviewer['headers'],json={'approve':True}).status_code == 200
    path=f"/api/v1/assets/{asset_id}/download"
    assert client.get(path,headers=reader['headers']).status_code == 403
    application=client.post('/api/v1/applications',headers=reader['headers'],json={'version_id':version['id'],
        'purpose':'隔离集成检查授权下载','duration_days':1,'idempotency_key':uuid.uuid4().hex})
    assert application.status_code == 201,application.text
    assert client.post(f"/api/v1/applications/{application.json()['id']}/decision",headers=owner['headers'],json={'approve':True}).status_code == 200
    assert client.get(path,headers=reader['headers']).status_code == 403
    assert client.post(f"/api/v1/versions/{version['id']}/accept-license",headers=reader['headers']).status_code == 200
    response=client.get(path,headers={**reader['headers'],'Range':'bytes=0-4095'})
    assert response.status_code==206 and response.content==raw[:4096]
    assert client.get(path,headers=actors['outsider']['headers']).status_code==403
    grant=client.get('/api/v1/grants',headers=owner['headers']).json()[0]
    assert client.post(f"/api/v1/grants/{grant['id']}/revoke",headers=owner['headers'],json={'reason':'集成验证完成'}).status_code==200
    assert client.get(path,headers=reader['headers']).status_code==403


def test_real_checksum_failure_is_not_clean(live):
    app,client,actors=live
    _,version=create(client,actors['owner'])
    asset_id=transfer(client,actors['owner'],version,b'x,y\na,b\n',digest='0'*64)
    with app.state.db.Session() as db:
        asset=db.get(Asset,asset_id)
        assert asset.status=='rejected' and asset.checks['integrity']=='failed'
    assert client.post(f"/api/v1/versions/{version['id']}/submit",headers=actors['owner']['headers']).status_code==409


def test_real_scanner_unavailable_keeps_quarantine(live):
    app,client,actors=live
    app.state.settings.clamav_port=1
    _,version=create(client,actors['owner'])
    asset_id=transfer(client,actors['owner'],version,b'x,y\na,b\n')
    with app.state.db.Session() as db:
        assert db.get(Asset,asset_id).status=='quarantine'
        assert db.scalar(select(Job).where(Job.parameters['asset_id'].as_string()==asset_id)).status=='failed'
    assert client.get(f"/api/v1/assets/{asset_id}/download",headers=actors['owner']['headers']).status_code==409
