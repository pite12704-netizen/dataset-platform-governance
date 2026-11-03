"""真实 API 状态流与 Worker 故障测试；这里的模拟复核不用于标记真实试验数据。"""
import io
import json
import uuid
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app.models import Asset, Dataset, ImageReviewBatch, ImageReviewEvent, ImageReviewItem, Job
from app.db import now
from app.tasks import run_job
from app.vision import Prediction
from test_lifecycle import approve, create_dataset, publish, upload


def setup_images(app, client, actors, count=1):
    app.state.settings.part_size_bytes = 1024 * 1024
    owner, reviewer = actors["owner"], actors["admin"]
    org = client.post("/api/v1/spaces", headers=owner["headers"], json={"name": "图像复核组织"}).json()
    response = client.post(f"/api/v1/spaces/{org['id']}/members", headers=owner["headers"],
                           json={"email": reviewer.get("email", "admin@example.test"), "role": "admin"})
    assert response.status_code == 200, response.text
    response = client.post(f"/api/v1/spaces/{org['id']}/members", headers=owner["headers"],
                           json={"email": actors["reader"].get("email", "reader@example.test"), "role": "reviewer"})
    assert response.status_code == 200, response.text
    ds, version = create_dataset(client, owner, visibility="organization", space=org["id"],modality='image')
    assets = []
    for index in range(count):
        out = io.BytesIO()
        Image.new("RGB", (64, 80), (index * 40, 100, 150)).save(out, "PNG")
        assets.append(upload(client, owner, version, out.getvalue(), f"person-{index}.png")[1]["asset_id"])
    report = {"schema_version": 1, "model_name": "test-model", "images": [
        {"asset_id": a, "width": 64, "height": 80, "persons": [{"id": "1", "bbox": [4, 5, 30, 60],
         "attributes": {k: {"label": v} for k, v in {"hat": "hat", "sleeve": "short_sleeves", "upper_color": "red"}.items()}}]}
        for a in assets]}
    asset = upload(client, owner, version, json.dumps(report).encode(), "image_pilot_report.json")[1]["asset_id"]
    return owner, reviewer, version, assets, asset


def import_batch(client, owner, report):
    response = client.post("/api/v1/image-reviews/import", headers=owner["headers"], json={"report_asset_id": report})
    assert response.status_code == 201, response.text
    batch = response.json()
    response = client.get("/api/v1/image-reviews/" + batch["id"], headers=owner["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def claim_token(client, actor, item):
    response = client.post(f"/api/v1/image-reviews/items/{item['id']}/claim", headers=actor["headers"])
    return response.json().get("claim_token", "0" * 32)


def finish_images(client,first,second,batch):
    root='/api/v1/image-reviews/'+batch['id']
    data=client.get(root,headers=first['headers']).json()
    for page in data['images']:
        path='/api/v1/image-reviews/images/'+page['id']
        claim=client.post(path+'/claim',headers=first['headers'])
        assert claim.status_code==200,claim.text
        row=claim.json()
        response=client.put(path,headers=first['headers'],json={'revision':row['revision'],
            'claim_token':row['claim_token'],'persons':[{'item_id':i['id'],'annotation':i['annotation']}
            for i in row['items'] if i['status']!='excluded']})
        assert response.status_code==200,response.text
        same=client.post(path+'/claim',headers=first['headers'])
        assert same.status_code==409
        claim=client.post(path+'/claim',headers=second['headers']);assert claim.status_code==200,claim.text
        row=claim.json()
        response=client.post(path+'/approve',headers=second['headers'],json={'revision':row['revision'],'claim_token':row['claim_token']})
        assert response.status_code==200,response.text


def transition(client, actor, item, action, revision):
    return client.post(f"/api/v1/image-reviews/items/{item['id']}/transition", headers=actor["headers"], json={
        "revision": revision, "claim_token": claim_token(client, actor, item), "action": action,
        "note": "测试中逐项核对", "human_confirmed": True})


def test_review_lifecycle_and_export_requires_separate_authorized_reviewer(app, client, actors):
    owner, reviewer, version, assets, report = setup_images(app, client, actors, 2)
    quality_reviewer = actors["reader"]
    batch = import_batch(client, owner, report)
    assert import_batch(client, owner, report)["id"] == batch["id"]
    item = batch["items"][0]
    root = "/api/v1/image-reviews/" + batch["id"]
    credential = client.post('/api/v1/credentials', headers=owner['headers'], json={
        'name': 'read-only-image-agent', 'scopes': ['content:read', 'agent:read'], 'duration_days': 1})
    assert credential.status_code == 201, credential.text
    token = credential.json()['token']
    assert client.get(root, headers={'Authorization': 'Bearer ' + token}).status_code == 403
    # 草稿只对数据负责人可见；正式版本发布也必须等待人物复核完成。
    assert client.get(root, headers=reviewer["headers"]).status_code == 403
    assert client.get(root + "/export", headers=owner["headers"]).status_code == 409
    assert client.get("/api/v1/image-reviews", headers=actors["stranger"]["headers"]).json() == []
    assert client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner["headers"]).status_code == 409
    edit_path = f"/api/v1/image-reviews/items/{item['id']}"
    assert client.put(edit_path, headers=owner["headers"], json={"revision": 0,
        "claim_token": "0" * 32, "annotation": item["annotation"], "note": "负责人不承担逐条标注"}).status_code == 403
    submitted = client.post(f"/api/v1/image-reviews/versions/{version['id']}/submit", headers=owner["headers"])
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["submitted_batches"] == 1
    # 审核人只有当前批次的受控图片预览权，没有版本下载或普通预览权限。
    reviewed = client.get(root, headers=reviewer["headers"])
    assert reviewed.status_code == 200, reviewed.text
    items = reviewed.json()["items"]
    assert client.get(f"/api/v1/assets/{assets[0]}/preview", headers=reviewer["headers"]).status_code == 403
    assert client.get(f"{root}/assets/{assets[0]}/preview", headers=reviewer["headers"]).status_code == 200
    assert transition(client, owner, items[0], "approve", 1).status_code == 403
    # 未修改的模型结果可直接审核通过，不强制填写说明。
    response = client.post(f"/api/v1/image-reviews/items/{items[0]['id']}/transition",
        headers=reviewer["headers"], json={"revision": 1, "claim_token": claim_token(client, reviewer, items[0]), "action": "approve"})
    assert response.status_code == 200, response.text
    # 结构化修改本身就是审计记录，不强迫填写文字原因；保存后直接等待第二位审核人确认。
    corrected = {**items[1]["annotation"], "labels": {**items[1]["annotation"]["labels"], "sleeve": "sleeveless"}}
    second_path = f"/api/v1/image-reviews/items/{items[1]['id']}"
    edited = client.put(second_path, headers=reviewer["headers"], json={"revision": 1,
        "claim_token": claim_token(client, reviewer, items[1]), "annotation": corrected})
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "submitted"
    assert transition(client, reviewer, items[1], "approve", 2).status_code == 403
    assert transition(client, quality_reviewer, items[1], "approve", 2).status_code == 200
    exported = client.get(root + "/export", headers=reviewer["headers"])
    assert exported.status_code == 200, exported.text
    data = exported.json()
    assert not data["partial"] and data["excluded_unapproved"] == 0
    assert len(data["annotations"]) == 2
    corrected_export = next(x for x in data["annotations"] if x["id"] == items[1]["id"])
    assert corrected_export["annotation"]["labels"]["sleeve"] == "sleeveless"
    assert corrected_export["reviewer_id"] == quality_reviewer["id"]
    history = client.get(second_path + "/history", headers=owner["headers"]).json()
    assert [h["action"] for h in history] == ["batch_submit", "edit", "approve"]
    assert history[1]["note"] == ""
    assert client.get(root, headers=owner["headers"]).json()["status"] == "completed"
    finish_images(client, reviewer, quality_reviewer, batch)
    publish(client, owner, reviewer, version)


def test_import_rejects_cross_version_assets_and_invalid_coordinates(app, client, actors):
    owner, reviewer, version, assets, report = setup_images(app, client, actors)
    _, other = create_dataset(client, owner)
    with app.state.db.Session() as db:
        asset = db.get(Asset, report)
        content = json.loads(b"".join(app.state.storage.chunks(asset.object_key)))
    foreign = upload(client, owner, other, json.dumps(content).encode(), "report.json")[1]["asset_id"]
    response = client.post("/api/v1/image-reviews/import", headers=owner["headers"], json={"report_asset_id": foreign})
    assert response.status_code == 422, response.text
    batch = import_batch(client, owner, report)
    assert client.post(f"/api/v1/image-reviews/{batch['id']}/submit", headers=owner["headers"]).status_code == 200
    item = batch["items"][0]
    invalid = {**item["annotation"], "bbox": [0, 0, 1000, 1000]}
    assert client.put(f"/api/v1/image-reviews/items/{item['id']}", headers=reviewer["headers"], json={
        "revision": 1, "claim_token": claim_token(client, reviewer, item), "annotation": invalid}).status_code == 422
    assert client.post(f"/api/v1/image-reviews/items/{item['id']}/transition", headers=owner["headers"], json={
        "revision": 0, "claim_token": "0" * 32, "action": "submit"}).status_code == 403


def submitted_review(app, client, actors, count=2):
    owner, reviewer, version, _, report = setup_images(app, client, actors, count)
    batch = import_batch(client, owner, report)
    assert client.post(f"/api/v1/image-reviews/{batch['id']}/submit", headers=owner['headers']).status_code == 200
    return owner, reviewer, actors['reader'], batch, version


def test_claims_distribute_work_and_repeated_claim_is_idempotent(app, client, actors):
    owner, first, second, batch, _ = submitted_review(app, client, actors)
    url = f"/api/v1/image-reviews/{batch['id']}/claim-next"
    a = client.post(url, headers=first['headers'], json={}).json()
    again = client.post(url, headers=first['headers'], json={}).json()
    assert a['item']['id'] == again['item']['id'] and a['claim_token'] == again['claim_token']
    b = client.post(url, headers=second['headers'], json={}).json()
    assert a['item']['id'] != b['item']['id']
    assert client.post(f"/api/v1/image-reviews/items/{a['item']['id']}/claim", headers=second['headers']).status_code == 409
    assert client.post(url, headers=owner['headers'], json={}).status_code == 403
    rows = client.get(f"/api/v1/image-reviews/{batch['id']}", headers=first['headers']).json()['items']
    assert all('claim_token' not in row for row in rows)


def test_expiry_fences_stale_windows_and_changes_require_independent_review(app, client, actors):
    owner, first, second, batch, _ = submitted_review(app, client, actors, 1)
    item = batch['items'][0]
    root = f"/api/v1/image-reviews/items/{item['id']}"
    old_token = claim_token(client, first, item)
    with app.state.db.Session() as db:
        db.get(ImageReviewItem, item['id']).claim_expires_at = now() - timedelta(seconds=1)
        db.commit()
    new_token = claim_token(client, second, item)
    assert new_token != old_token
    assert client.post(root + '/renew', headers=first['headers'], json={'claim_token': old_token}).status_code == 409
    assert client.post(root + '/release', headers=first['headers'], json={'claim_token': old_token}).status_code == 200
    body = {'revision': 1, 'claim_token': old_token, 'annotation': item['annotation']}
    assert client.put(root, headers=first['headers'], json=body).status_code == 409
    body['claim_token'] = new_token
    assert client.put(root, headers=second['headers'], json=body).status_code == 200
    assert client.put(root, headers=second['headers'], json=body).status_code == 409
    assert transition(client, second, item, 'approve', 2).status_code == 403
    assert transition(client, first, item, 'approve', 2).status_code == 200
    assert client.post(root + '/transition', headers=first['headers'], json={
        'revision': 2, 'claim_token': old_token, 'action': 'approve'}).status_code == 409
    events = client.get(root + '/history', headers=owner['headers']).json()
    assert [event['action'] for event in events] == ['batch_submit', 'edit', 'approve']
    assert client.get(f"/api/v1/image-reviews/{batch['id']}", headers=owner['headers']).json()['status'] == 'completed'


def test_renew_release_switch_and_stale_revision(app, client, actors):
    _, first, second, batch, _ = submitted_review(app, client, actors)
    first_item, other = batch['items']
    token = claim_token(client, first, first_item)
    root = f"/api/v1/image-reviews/items/{first_item['id']}"
    renewal = client.post(root + '/renew', headers=first['headers'], json={'claim_token': token})
    assert renewal.status_code == 200
    assert client.put(root, headers=first['headers'], json={
        'revision': 0, 'claim_token': token, 'annotation': first_item['annotation']}).status_code == 409
    other_token = claim_token(client, first, other)
    assert other_token != token
    assert claim_token(client, second, first_item) != '0' * 32
    assert client.post(f"/api/v1/image-reviews/items/{other['id']}/release", headers=first['headers'],
                       json={'claim_token': other_token}).status_code == 200
    assert claim_token(client, second, other) != '0' * 32


def test_duplicate_batches_cannot_both_be_submitted(app, client, actors):
    owner, _, _, batch, version = submitted_review(app, client, actors, 1)
    from app.image_reviews import create_batch
    row = batch['items'][0]
    with app.state.db.Session() as db:
        duplicate = create_batch(db, version['id'], owner['id'], [{
            'asset_id': row['asset_id'], 'person_key': '1', 'width': row['width'], 'height': row['height'],
            'annotation': row['annotation']}], {'origin': 'test-repeat-model'})
        db.commit()
        duplicate_id = duplicate.id
    assert client.post(f"/api/v1/image-reviews/{duplicate_id}/submit", headers=owner['headers']).status_code == 409
    assert client.post(f"/api/v1/image-reviews/versions/{version['id']}/submit", headers=owner['headers']).status_code == 409
    assert client.get(f"/api/v1/image-reviews/{duplicate_id}", headers=owner['headers']).json()['status'] == 'draft'


def test_missing_lease_or_removed_reviewer_cannot_save_or_renew(app, client, actors):
    owner, reviewer, _, batch, version = submitted_review(app, client, actors, 1)
    item = batch['items'][0]
    root = f"/api/v1/image-reviews/items/{item['id']}"
    assert client.put(root, headers=reviewer['headers'], json={
        'revision': 1, 'annotation': item['annotation']}).status_code == 422
    token = claim_token(client, reviewer, item)
    with app.state.db.Session() as db:
        space_id = db.get(Dataset, version['dataset_id']).space_id
    assert client.post(f'/api/v1/spaces/{space_id}/members', headers=owner['headers'], json={
        'email': 'admin@example.test', 'role': 'reviewer', 'active': False}).status_code == 200
    assert client.post(root + '/renew', headers=reviewer['headers'], json={'claim_token': token}).status_code == 403
    assert client.put(root, headers=reviewer['headers'], json={
        'revision': 1, 'claim_token': token, 'annotation': item['annotation']}).status_code == 403


def prediction(*args):
    return Prediction(persons=[{"bbox": [100, 100, 900, 900], "labels": {
        "hat": "no_hat", "sleeve": "unclear", "upper_color": "other_or_unclear"}}], truncated=False), {"seconds": 0.1}


@pytest.mark.parametrize("failure", ["model_error", "cancel", "blocked_dataset"])
def test_vision_worker_atomic_retry_and_idempotency(app, client, actors, monkeypatch, failure):
    owner, _, version, _, _ = setup_images(app, client, actors, 2)
    app.state.settings.vision_enabled = True
    app.state.settings.model_url = "http://127.0.0.1:1234/v1"
    app.state.settings.model_name = "mock-vision"
    app.state.settings.task_mode = "manual"
    data = {"version_id": version["id"], "kind": "vision", "idempotency_key": uuid.uuid4().hex}
    response = client.post("/api/v1/jobs", headers=owner["headers"], json=data)
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    calls = []
    def failing(*args):
        calls.append(1)
        if len(calls) == 2:
            if failure == "model_error":
                raise RuntimeError("test model unavailable")
            with app.state.db.Session() as db:
                if failure == "cancel":
                    db.get(Job, job_id).status = "cancelled"
                else:
                    db.get(Dataset, version["dataset_id"]).status = "blocked"
                db.commit()
        return prediction()
    monkeypatch.setattr("app.vision.predict", failing)
    run_job(job_id, app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        assert db.get(Job, job_id).status == ("failed" if failure == "model_error" else "cancelled")
        assert db.scalar(select(ImageReviewBatch)) is None
        assert db.scalar(select(ImageReviewItem)) is None
        if failure == "blocked_dataset":
            db.get(Dataset, version["dataset_id"]).status = "active"
            db.commit()
    monkeypatch.setattr("app.vision.predict", prediction)
    assert client.post(f"/api/v1/jobs/{job_id}/retry", headers=owner["headers"]).status_code == 200
    run_job(job_id, app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        assert db.get(Job, job_id).status == "completed"
        assert len(db.scalars(select(ImageReviewBatch)).all()) == 1
        assert len(db.scalars(select(ImageReviewItem)).all()) == 2
        assert db.scalar(select(ImageReviewEvent)) is None
    assert client.post("/api/v1/jobs", headers=owner["headers"], json=data).json()["id"] == job_id
    run_job(job_id, app.state.db, app.state.storage, app.state.settings)
    with app.state.db.Session() as db:
        assert len(db.scalars(select(ImageReviewBatch)).all()) == 1


def test_vision_strict_model_output():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Prediction.model_validate({"persons": [{"bbox": [0, 0, 1001, 20], "labels": {
            "hat": "hat", "sleeve": "short_sleeves", "upper_color": "red"}}], "truncated": False})


@pytest.mark.parametrize('url',['http://169.254.169.254/v1','http://0.0.0.0/v1','http://224.0.0.1/v1','http://user:secret@127.0.0.1/v1','http://127.0.0.1/v1?token=secret'])
def test_vision_rejects_unsafe_configured_destination(app,url):
    from app.vision import configured
    from fastapi import HTTPException
    cfg=app.state.settings;cfg.vision_enabled=True;cfg.model_name='test';cfg.model_url=url
    with pytest.raises(HTTPException):configured(cfg)


def test_production_vision_requires_explicit_weight_revision():
    from test_enterprise_operations import production
    from app.vision import configured
    from fastapi import HTTPException
    cfg=production(vision_enabled=True,model_url='http://127.0.0.1:1234/v1',model_name='alias')
    with pytest.raises(HTTPException):configured(cfg)
    cfg.vision_model_revision='operator-verified-test-sha256'
    configured(cfg)
