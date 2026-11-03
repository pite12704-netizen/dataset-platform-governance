"""Publication policy boundaries independent of the image-review UI."""
import pytest
from sqlalchemy import select

from app.models import ImageReviewBatch, Version
from test_lifecycle import create_dataset, publish, upload


@pytest.mark.parametrize("published", [False, True])
def test_blocked_dataset_cannot_be_previewed_by_owner_or_reviewer(client, actors, published):
    owner, admin = actors["owner"], actors["admin"]
    ds, version = create_dataset(client, owner, access="open")
    _, asset = upload(client, owner, version, preview=True)
    if published:
        publish(client, owner, admin, version)
    else:
        response = client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner["headers"])
        assert response.status_code == 200, response.text
    path = f"/api/v1/assets/{asset['asset_id']}/preview"
    assert client.get(path, headers=owner["headers"]).status_code == 200
    assert client.get(path, headers=admin["headers"]).status_code == 200
    response = client.post(f"/api/v1/admin/datasets/{ds['id']}/status", headers=admin["headers"],
                           json={"status": "blocked", "reason": "紧急暂停内容访问"})
    assert response.status_code == 200, response.text
    for actor in [owner, admin, actors["reader"]]:
        assert client.get(path, headers=actor["headers"]).status_code == 403
    assert client.get(path).status_code == 403


def test_publication_decision_rechecks_late_review_batch(client, actors, app):
    owner, admin = actors["owner"], actors["admin"]
    _, version = create_dataset(client, owner)
    upload(client, owner, version)
    response = client.post(f"/api/v1/versions/{version['id']}/submit", headers=owner["headers"])
    assert response.status_code == 200, response.text
    publication = response.json()
    # A vision worker may finish after the publication request was submitted.
    with app.state.db.Session() as db:
        db.add(ImageReviewBatch(version_id=version["id"], creator_id=owner["id"],
                                provenance={"origin": "test-worker"}, status="draft"))
        db.commit()
    response = client.post(f"/api/v1/publications/{publication['id']}/decision",
                           headers=admin["headers"], json={"approve": True})
    assert response.status_code == 409, response.text
    with app.state.db.Session() as db:
        assert db.get(Version, version["id"]).status == "pending_review"
        batch = db.scalar(select(ImageReviewBatch).where(ImageReviewBatch.version_id == version["id"]))
        batch.status = "completed"
        db.commit()
    assert client.post(f"/api/v1/publications/{publication['id']}/decision",
                       headers=admin["headers"], json={"approve": True}).status_code == 200
