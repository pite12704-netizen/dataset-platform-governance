"""Real MySQL locking regressions; the imported fixture enforces a dedicated test database."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from fastapi.testclient import TestClient

from app import datasets
from app.models import Dataset, Version
from test_lifecycle import create_dataset, upload
# Imported fixture enforces the dedicated MySQL database and account.
from test_review_mysql import mysql_case  # noqa: F401


def test_lock_refreshes_cached_permission_metadata(mysql_case):
    app, client, actors = mysql_case
    ds, version = create_dataset(client, actors['owner'])
    from app.workflows import locked_version
    with app.state.db.Session() as first:
        stale = first.get(Dataset, ds['id'])
        assert stale.status == 'active'
        with app.state.db.Session() as second:
            second.get(Dataset, ds['id']).status = 'blocked'
            second.commit()
        _, locked = locked_version(first, version['id'])
        assert locked.status == 'blocked'


def test_catalog_visibility_edit_cannot_race_organization_publication(mysql_case, monkeypatch):
    app, client, actors = mysql_case
    owner, reviewer = actors["owner"], actors["reader"]
    response = client.post("/api/v1/spaces", headers=owner["headers"], json={"name": "publication-race"})
    assert response.status_code == 201, response.text
    space = response.json()
    response = client.post(f"/api/v1/spaces/{space['id']}/members", headers=owner["headers"],
                           json={"email": reviewer["email"], "role": "admin"})
    assert response.status_code == 200, response.text
    ds, version = create_dataset(client, owner, visibility="organization", space=space["id"])
    upload(client, owner, version)

    editing_checked, release_edit = Event(), Event()
    original = datasets.validate_audience

    def pause_edit(db, data, settings):
        original(db, data, settings)
        # edit_dataset has locked Dataset and checked that no frozen version exists.
        editing_checked.set()
        assert release_edit.wait(10), "test did not release catalog edit"

    monkeypatch.setattr(datasets, "validate_audience", pause_edit)

    def request(method, path, actor, payload=None):
        from app.main import create_app
        with TestClient(create_app(app.state.settings)) as thread_client:
            return thread_client.request(method, path, headers=actor["headers"], json=payload)

    review_reply = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        edited = executor.submit(request, "PUT", f"/api/v1/datasets/{ds['id']}", owner,
            {"space_id": space["id"], "name": "public-after-edit", "visibility": "public", "access_mode": "open"})
        try:
            assert editing_checked.wait(10), "catalog edit did not reach the synchronization point"
            submitted = executor.submit(request, "POST", f"/api/v1/versions/{version['id']}/submit", owner)
            try:
                reply = submitted.result(timeout=1)
            except TimeoutError:
                # Correct implementation waits for the Dataset lock before observing policy.
                reply = None
            else:
                # The vulnerable implementation can submit and approve under the old visibility.
                assert reply.status_code == 200, reply.text
                review_reply = request("POST", f"/api/v1/publications/{reply.json()['id']}/decision",
                                       reviewer, {"approve": True})
        finally:
            release_edit.set()
        edit_reply = edited.result(timeout=10)
        reply = submitted.result(timeout=10)
    assert edit_reply.status_code == 200, edit_reply.text
    assert reply.status_code == 200, reply.text
    if review_reply is None:
        review_reply = request("POST", f"/api/v1/publications/{reply.json()['id']}/decision",
                               reviewer, {"approve": True})
    assert review_reply.status_code == 403, review_reply.text
    with app.state.db.Session() as db:
        assert db.get(Version, version["id"]).status == "pending_review"
