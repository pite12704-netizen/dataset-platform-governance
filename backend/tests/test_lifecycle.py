import hashlib
from datetime import timedelta

from sqlalchemy import select

from app.db import now
from app.models import Grant, Job, Space, User, Version


def create_dataset(client, actor, access="application", visibility="public", space=None,modality='text'):
    headers = actor["headers"]
    response = client.post("/api/v1/datasets", headers=headers, json={"name": "训练语料", "description": "模型训练数据",
        "space_id": space or actor["space_id"], "visibility": visibility, "access_mode": access,"modality":modality})
    assert response.status_code == 201, response.text
    ds = response.json()
    response = client.post(f"/api/v1/datasets/{ds['id']}/versions", headers=headers,
                           json={"license": "CC-BY-4.0", "source": "自有合成测试样本"})
    assert response.status_code == 201, response.text
    return ds, response.json()


def upload(client, actor, version, data=b"question,answer\nhello,world\n", name="train.csv", preview=False, digest=None):
    import uuid
    response = client.post(f"/api/v1/versions/{version['id']}/uploads", headers=actor["headers"], json={
        "filename": name, "size": len(data), "sha256": digest or hashlib.sha256(data).hexdigest(),
        "idempotency_key": uuid.uuid4().hex, "preview_allowed": preview})
    assert response.status_code == 201, response.text
    session = response.json()
    for n, offset in enumerate(range(0, len(data), session["part_size"]), 1):
        response = client.put(f"/api/v1/uploads/{session['id']}/parts/{n}", headers=actor["headers"], content=data[offset:offset + session["part_size"]])
        assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/uploads/{session['id']}/complete", headers=actor["headers"])
    assert response.status_code == 200, response.text
    return session, response.json()


def publish(client, owner, admin, version):
    r = client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner["headers"])
    assert r.status_code == 200, r.text
    p = r.json()
    r = client.post(f"/api/v1/publications/{p['id']}/decision", headers=admin["headers"], json={"approve": True})
    assert r.status_code == 200, r.text
    return p


def approve(client, owner, reader, version, project=None):
    import uuid
    payload = {"version_id": version["id"], "purpose": "研究模型泛化能力", "duration_days": 7, "idempotency_key": uuid.uuid4().hex}
    if project:
        payload["project_id"] = project
    r = client.post("/api/v1/applications", headers=reader["headers"], json=payload)
    assert r.status_code == 201, r.text
    app = r.json()
    r = client.post(f"/api/v1/applications/{app['id']}/decision", headers=owner["headers"], json={"approve": True})
    assert r.status_code == 200, r.text
    return app, payload


def test_complete_application_lifecycle(client, actors):
    owner, reader, admin = [actors[x] for x in ["owner", "reader", "admin"]]
    ds, version = create_dataset(client, owner)
    assert client.get("/api/v1/datasets").json()["total"] == 0
    _, result = upload(client, owner, version)
    asset_id = result["asset_id"]
    publish(client, owner, admin, version)
    assert client.get("/api/v1/datasets").json()["total"] == 1
    path = f"/api/v1/assets/{asset_id}/download"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=reader["headers"]).status_code == 403
    app, payload = approve(client, owner, reader, version)
    # Approval is not license acceptance.
    assert client.get(path, headers=reader["headers"]).status_code == 403
    assert client.post(f"/api/v1/versions/{version['id']}/accept-license", headers=reader["headers"]).status_code == 200
    response = client.get(path, headers={**reader["headers"], "Range": "bytes=0-7"})
    assert response.status_code == 206 and response.content == b"question"
    assert client.get(path, headers={**reader["headers"], "Range": "bytes=999-"}).status_code == 416
    assert client.get(path, headers=admin["headers"]).status_code == 403
    # Idempotency and completed decisions cannot create additional grants.
    assert client.post("/api/v1/applications", headers=reader["headers"], json=payload).json()["id"] == app["id"]
    assert client.post(f"/api/v1/applications/{app['id']}/decision", headers=owner["headers"], json={"approve": True}).status_code == 409
    grants = client.get("/api/v1/grants", headers=owner["headers"]).json()
    assert len(grants) == 1
    assert client.post(f"/api/v1/grants/{grants[0]['id']}/revoke", headers=owner["headers"], json={"reason": "研究任务已经结束"}).status_code == 200
    assert client.get(path, headers=reader["headers"]).status_code == 403


def test_open_access_preview_and_immutability(client, actors):
    owner, reader, admin = [actors[x] for x in ["owner", "reader", "admin"]]
    ds, version = create_dataset(client, owner, access="open")
    _, result = upload(client, owner, version, preview=True)
    preview = f"/api/v1/assets/{result['asset_id']}/preview"
    assert client.get(preview).status_code == 403
    publish(client, owner, admin, version)
    assert client.get(preview).status_code == 200
    client.post(f"/api/v1/versions/{version['id']}/accept-license", headers=reader["headers"])
    assert client.get(f"/api/v1/assets/{result['asset_id']}/download", headers=reader["headers"]).status_code == 200
    assert client.put(f"/api/v1/versions/{version['id']}", headers=owner["headers"], json={"license": "changed", "source": "changed"}).status_code == 409
    assert client.post(f"/api/v1/versions/{version['id']}/uploads", headers=owner["headers"], json={
        "filename": "new.txt", "size": 3, "sha256": "a" * 64, "idempotency_key": "new-upload-key"}).status_code == 409


def test_self_approval_rejected_and_resubmission(client, actors):
    owner, admin = actors["owner"], actors["admin"]
    ds, version = create_dataset(client, owner)
    upload(client, owner, version)
    p = client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner["headers"]).json()
    assert client.post(f"/api/v1/publications/{p['id']}/decision", headers=owner["headers"], json={"approve": True}).status_code == 403
    assert client.post(f"/api/v1/publications/{p['id']}/decision", headers=admin["headers"], json={"approve": False, "comment": "补全数据说明"}).status_code == 200
    assert client.put(f"/api/v1/versions/{version['id']}", headers=owner["headers"], json={"license": "CC-BY-4.0", "source": "完整来源"}).status_code == 200
    publish(client, owner, admin, version)


def test_checksum_and_sensitive_data_block_publication(client, actors):
    owner = actors["owner"]
    _, version = create_dataset(client, owner)
    _, result = upload(client, owner, version, digest="0"*64)
    v = client.get(f"/api/v1/versions/{version['id']}", headers=owner["headers"]).json()
    assert v["assets"][0]["status"] == "rejected"
    assert client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner["headers"]).status_code == 409
    _, v2 = create_dataset(client, owner)
    upload(client, owner, v2, b"Email: someone@example.test", "sensitive.txt")
    assert client.post(f"/api/v1/versions/{v2['id']}/submit", headers=owner["headers"]).status_code == 409


def test_cross_organization_isolation(client, actors):
    owner, reader, admin = [actors[x] for x in ["owner", "reader", "admin"]]
    org = client.post("/api/v1/spaces", headers=owner["headers"], json={"name": "组织甲"}).json()
    client.post(f"/api/v1/spaces/{org['id']}/members", headers=owner["headers"], json={"email":"admin@example.test","role":"admin"})
    ds, v = create_dataset(client, owner, visibility="organization", space=org["id"])
    _, result = upload(client, owner, v)
    publish(client, owner, admin, v)
    assert client.get(f"/api/v1/datasets/{ds['id']}", headers=reader["headers"]).status_code == 404
    assert client.get(f"/api/v1/versions/{v['id']}", headers=reader["headers"]).status_code == 404
    assert client.get(f"/api/v1/assets/{result['asset_id']}/preview", headers=reader["headers"]).status_code == 403
    assert client.get(f"/api/v1/stats?space_id={org['id']}", headers=reader["headers"]).status_code == 403
    assert client.get(f"/api/v1/audits?space_id={org['id']}", headers=reader["headers"]).status_code == 403
    # Being an org reviewer gives no content grant.
    assert client.get(f"/api/v1/assets/{result['asset_id']}/download", headers=admin["headers"]).status_code == 403


def test_project_grant_revoked_by_membership(client, actors):
    owner, reader, admin = [actors[x] for x in ["owner", "reader", "admin"]]
    ds, v = create_dataset(client, owner)
    _, result = upload(client, owner, v)
    publish(client, owner, admin, v)
    org = client.post("/api/v1/spaces", headers=reader["headers"], json={"name":"研究组织"}).json()
    project = client.post(f"/api/v1/spaces/{org['id']}/projects", headers=reader["headers"], json={"name":"研究项目"}).json()
    approve(client, owner, reader, v, project["id"])
    client.post(f"/api/v1/versions/{v['id']}/accept-license", headers=reader["headers"])
    path = f"/api/v1/assets/{result['asset_id']}/download"
    assert client.get(path, headers=reader["headers"]).status_code == 200
    client.post(f"/api/v1/spaces/{org['id']}/projects/{project['id']}/members", headers=reader["headers"], json={"user_id":reader["id"],"active":False})
    assert client.get(path, headers=reader["headers"]).status_code == 403


def test_grant_expiry_and_new_version(client, actors, app):
    owner, reader, admin = [actors[x] for x in ["owner", "reader", "admin"]]
    ds, v = create_dataset(client, owner)
    _, result = upload(client, owner, v)
    publish(client, owner, admin, v)
    approve(client, owner, reader, v)
    client.post(f"/api/v1/versions/{v['id']}/accept-license", headers=reader["headers"])
    with app.state.db.Session() as db:
        grant = db.scalar(select(Grant))
        grant.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.get(f"/api/v1/assets/{result['asset_id']}/download", headers=reader["headers"]).status_code == 403


def test_upload_resume_duplicate_names_and_quota(client, actors, app):
    owner = actors["owner"]
    ds, v = create_dataset(client, owner)
    session, result = upload(client, owner, v)
    assert client.post(f"/api/v1/uploads/{session['id']}/complete", headers=owner["headers"]).json()["asset_id"] == result["asset_id"]
    _, second = upload(client, owner, v)
    assert second["asset_id"] != result["asset_id"]
    assert client.get(f"/api/v1/uploads/{session['id']}", headers=actors["stranger"]["headers"]).status_code == 404
    with app.state.db.Session() as db:
        space = db.get(Space, owner["space_id"])
        assert space.used_bytes == 2 * len(b"question,answer\nhello,world\n") and space.reserved_bytes == 0
        space.quota_bytes = space.used_bytes + 1
        db.commit()
    response = client.post(f"/api/v1/versions/{v['id']}/uploads", headers=owner["headers"], json={"filename":"big.txt","size":20,"sha256":"1"*64,"idempotency_key":"larger-than-quota"})
    assert response.status_code == 409


def test_jobs_quality_conversion_and_package(client, actors):
    owner = actors["owner"]
    ds, v = create_dataset(client, owner)
    upload(client, owner, v)
    for kind in ["quality", "convert", "package"]:
        payload = {"version_id":v["id"],"kind":kind,"idempotency_key":"job-test-"+kind}
        if kind == "convert":
            payload["target_format"] = "json"
        r = client.post("/api/v1/jobs", headers=owner["headers"], json=payload)
        assert r.status_code == 201, r.text
        job = r.json()
        assert job["status"] == "completed", job
        assert client.post("/api/v1/jobs", headers=owner["headers"], json=payload).json()["id"] == job["id"]
        assert client.get(f"/api/v1/jobs/{job['id']}", headers=actors["stranger"]["headers"]).status_code == 404
        if kind == "convert":
            derived = client.get(f"/api/v1/versions/{job['result']['version_id']}", headers=owner["headers"]).json()
            assert derived["parent_version_id"] == v["id"] and derived["status"] == "draft"
            assert derived["assets"][0]["status"] == "clean"
        if kind == "package":
            assert client.get(f"/api/v1/jobs/{job['id']}/download", headers=owner["headers"]).content[:2] == b"PK"


def test_revoked_access_cancels_queued_job(client, actors, app):
    owner, reader, admin = [actors[x] for x in ["owner", "reader", "admin"]]
    _, v = create_dataset(client, owner)
    upload(client, owner, v)
    publish(client, owner, admin, v)
    approve(client, owner, reader, v)
    client.post(f"/api/v1/versions/{v['id']}/accept-license", headers=reader["headers"])
    app.state.settings.task_mode = "manual"
    job = client.post("/api/v1/jobs", headers=reader["headers"], json={"version_id":v["id"],"kind":"quality","idempotency_key":"queued-auth-test"}).json()
    with app.state.db.Session() as db:
        db.scalar(select(Grant)).revoked_at = now()
        db.commit()
    from app.tasks import run_job
    run_job(job["id"], app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        assert db.get(Job, job["id"]).status == "cancelled"
