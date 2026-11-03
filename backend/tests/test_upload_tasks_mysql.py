"""真实 MySQL：慢分片等待期间无行锁，读请求与其他分片仍可推进。"""
import asyncio
import hashlib
import uuid

import httpx
from sqlalchemy import event

from test_lifecycle import create_dataset
from test_review_mysql import mysql_case  # noqa: F401
from test_upload_task_resilience import test_package_uses_frozen_input_and_excludes_later_quarantine as frozen_case
from test_upload_task_resilience import test_crash_after_object_write_reclaims_previous_attempt as crash_case
from test_concurrency_boundaries import test_upload_reauthenticates_after_stream_and_never_holds_session_while_receiving as revoke_case


def test_mysql_slow_part_does_not_hold_locks_or_block_other_part(mysql_case):
    app, client, actors = mysql_case
    owner = actors['owner']; _, version = create_dataset(client, owner)
    app.state.settings.part_size_bytes = 16
    body = b'x' * 32
    response = client.post(f"/api/v1/versions/{version['id']}/uploads", headers=owner['headers'], json={
        'filename': 'slow.txt', 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'idempotency_key': uuid.uuid4().hex})
    assert response.status_code == 201, response.text
    session = response.json(); queries = []
    def collect(_, __, statement, *args):
        queries.append(statement)
    def short_lock_timeout(connection, *args):
        cursor = connection.cursor(); cursor.execute('SET SESSION innodb_lock_wait_timeout=2'); cursor.close()
    event.listen(app.state.db.engine, 'before_cursor_execute', collect)
    event.listen(app.state.db.engine, 'checkout', short_lock_timeout)
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_body():
            started.set()
            await release.wait()
            yield body[:16]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
            slow = asyncio.create_task(api.put(f"/api/v1/uploads/{session['id']}/parts/1",
                headers=owner['headers'], content=delayed_body()))
            try:
                await asyncio.wait_for(started.wait(), 5)
                assert not any('FOR UPDATE' in query.upper() for query in queries)
                read, second = await asyncio.wait_for(asyncio.gather(
                    api.get(f"/api/v1/uploads/{session['id']}", headers=owner['headers']),
                    api.put(f"/api/v1/uploads/{session['id']}/parts/2", headers=owner['headers'], content=body[16:])), 5)
                assert read.status_code == second.status_code == 200
            finally:
                release.set()
                first = await asyncio.wait_for(slow, 5)
            assert first.status_code == 200, first.text
    try:
        client.portal.call(scenario)
        persisted = client.get(f"/api/v1/uploads/{session['id']}", headers=owner['headers']).json()
        assert set(persisted['parts']) == {'1', '2'}
    finally:
        event.remove(app.state.db.engine, 'before_cursor_execute', collect)
        event.remove(app.state.db.engine, 'checkout', short_lock_timeout)


def test_mysql_frozen_package_input(mysql_case):
    frozen_case(*mysql_case)


def test_mysql_worker_object_crash_recovery(mysql_case, monkeypatch):
    crash_case(*mysql_case, monkeypatch)


def test_mysql_upload_midstream_session_revocation(mysql_case, monkeypatch):
    app, client, actors = mysql_case
    revoke_case(client, actors, app, monkeypatch)


def test_mysql_pool_exhaustion_returns_bounded_503_and_recovers(mysql_case):
    from app.db import Database
    from sqlalchemy import text
    app, client, _ = mysql_case
    settings = app.state.settings.model_copy(update={"database_pool_size": 1,
        "database_max_overflow": 0, "database_pool_timeout": .1})
    database = Database(settings.database_url, settings)
    @app.get("/api/v1/test-pool-limit")
    def acquire():
        with database.engine.connect() as connection:
            return {"value": connection.scalar(text("SELECT 1"))}
    try:
        with database.engine.connect():
            response = client.get("/api/v1/test-pool-limit")
            assert response.status_code == 503 and response.json()["error"] == "database_busy"
        assert client.get("/api/v1/test-pool-limit").json() == {"value": 1}
    finally:
        database.engine.dispose()
