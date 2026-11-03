"""并发行为回归：事件循环、会话归属、撤销认证、依赖背压与统计隔离。"""
import asyncio
import hashlib
import logging
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout

from app.agent import MCPKeyResolver
from app.db import run_db
from app.models import Identity, LoginSession, Upload, User
from test_lifecycle import create_dataset, upload, publish, approve


@pytest.mark.parametrize("kind", ["mcp", "oidc"])
def test_slow_sync_operation_does_not_block_liveness(app, monkeypatch, kind):
    started, release = threading.Event(), threading.Event()
    thread_ids = []
    def block(*args):
        thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(3), "test failed to release worker"
        if kind == "mcp":
            return SimpleNamespace(id="user"), {"catalog:read"}
        return Response("logged in")
    if kind == "mcp":
        monkeypatch.setattr("app.agent.mcp_identity", block)
    else:
        async def exchange(request):
            return {"userinfo": {}}
        app.state.oauth = SimpleNamespace(enterprise=SimpleNamespace(authorize_access_token=exchange))
        monkeypatch.setattr("app.auth.finish_oidc_login", block)

    async def scenario():
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
            slow = asyncio.create_task(session.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                if kind == "mcp" else session.get("/api/v1/auth/oidc/callback"))
            try:
                async with asyncio.timeout(1):
                    while not started.is_set():
                        await asyncio.sleep(.005)
                assert thread_ids == [thread_ids[0]] and thread_ids[0] != loop_thread
                assert not slow.done()
                response = await asyncio.wait_for(session.get("/health/live"), .5)
                assert response.status_code == 200 and not slow.done()
            finally:
                release.set()
                response = await slow
            assert response.status_code == 200
    asyncio.run(scenario())


def test_run_db_owns_session_in_one_thread_and_closes_on_error(app, monkeypatch):
    phases = []
    @contextmanager
    def session():
        marker = object()
        phases.append(("open", threading.get_ident()))
        try:
            yield marker
        finally:
            phases.append(("close", threading.get_ident()))
    monkeypatch.setattr(app.state.db, "Session", session)
    def fail(db):
        phases.append(("operation", threading.get_ident()))
        raise ValueError("rollback required")
    loop_thread = threading.get_ident()
    with pytest.raises(ValueError, match="rollback required"):
        asyncio.run(run_db(SimpleNamespace(app=app), fail))
    assert [phase for phase, _ in phases] == ["open", "operation", "close"]
    assert len({identifier for _, identifier in phases}) == 1
    assert phases[0][1] != loop_thread


def test_run_db_failure_rolls_back_uncommitted_records(app):
    def fail(db):
        db.add(User(email="rolled-back@example.test", name="rollback", password_hash="unused"))
        db.flush()
        raise ValueError("fail after flush")
    with pytest.raises(ValueError):
        asyncio.run(run_db(SimpleNamespace(app=app), fail))
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_upload_reauthenticates_after_stream_and_never_holds_session_while_receiving(client, actors, app, monkeypatch):
    owner = actors["owner"]
    _, version = create_dataset(client, owner)
    data = b"a,b\n1,2\n"
    result = client.post(f"/api/v1/versions/{version['id']}/uploads", headers=owner["headers"], json={
        "filename": "part.csv", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "idempotency_key": "stream-revocation"})
    assert result.status_code == 201
    upload_id = result.json()["id"]
    original_factory = app.state.db.Session
    active = set()
    @contextmanager
    def tracked_session():
        with original_factory() as db:
            active.add(id(db))
            try:
                yield db
            finally:
                active.remove(id(db))
    monkeypatch.setattr(app.state.db, "Session", tracked_session)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
            async def body():
                assert not active, "a DB session leaked into upload reception"
                yield data[:2]
                response = await session.post("/api/v1/auth/logout", headers=owner["headers"])
                assert response.status_code == 200
                assert not active
                yield data[2:]
            return await session.put(f"/api/v1/uploads/{upload_id}/parts/1", headers=owner["headers"], content=body())
    response = client.portal.call(scenario)
    assert response.status_code == 401
    with original_factory() as db:
        assert db.get(Upload, upload_id).parts == {}
    assert not active


@pytest.mark.parametrize("exception,error", [(PoolTimeout("private pool DSN"), "database_busy"),
    (OperationalError("private SQL", {"password": "private-password"}, Exception("private server")), "database_unavailable")])
def test_database_backpressure_has_safe_response_and_correlated_logs(client, app, caplog, exception, error):
    @app.get("/api/v1/test-db-backpressure")
    def fail():
        raise exception
    with caplog.at_level(logging.WARNING, logger="platform.requests"):
        response = client.get("/api/v1/test-db-backpressure")
    assert response.status_code == 503 and response.json()["error"] == error
    assert response.headers["retry-after"] == "1"
    assert response.headers["x-request-id"] in caplog.text
    for secret in ("private pool DSN", "private SQL", "private-password", "private server"):
        assert secret not in caplog.text and secret not in response.text


def test_mcp_parse_error_does_not_bypass_auth_and_json_null_is_not_parse_error(client, monkeypatch):
    def deny(*args):
        raise HTTPException(401, "authentication required")
    monkeypatch.setattr("app.agent.mcp_identity", deny)
    assert client.post("/mcp", content=b"{").status_code == 401
    monkeypatch.setattr("app.agent.mcp_identity", lambda *args: (SimpleNamespace(id="user"), set()))
    response = client.post("/mcp", content=b"{")
    assert response.status_code == 400 and response.json()["error"]["code"] == -32700
    response = client.post("/mcp", content=b"null")
    assert response.status_code == 400 and response.json()["error"] == "request_failed"
    assert client.post("/mcp", headers={"Origin": "https://evil.example"}, content=b"{").status_code == 403


def test_jwks_cache_reused_and_outage_backoff_is_bounded(app, monkeypatch):
    settings = app.state.settings
    settings.mcp_jwks_url = "https://identity.example/jwks"
    clients, calls = [], []
    def factory(url, **options):
        clients.append(options)
        def get(token):
            calls.append(token)
            if token == "unavailable":
                raise jwt.PyJWKClientConnectionError("private URL")
            return "verified-key"
        return SimpleNamespace(get_signing_key_from_jwt=get)
    monkeypatch.setattr("app.agent.jwt.PyJWKClient", factory)
    resolver = MCPKeyResolver()
    assert resolver.signing_key("one", settings) == resolver.signing_key("two", settings)
    assert len(clients) == 1 and clients[0]["timeout"] == 3 and not clients[0]["cache_keys"]
    with pytest.raises(jwt.PyJWKClientConnectionError):
        resolver.signing_key("unavailable", settings)
    with pytest.raises(jwt.PyJWKClientConnectionError):
        resolver.signing_key("retry", settings)
    assert calls == ["one", "two", "unavailable"]
    settings.mcp_jwks_url = "https://identity2.example/jwks"
    assert resolver.signing_key("three", settings) == "verified-key" and len(clients) == 2


def test_mcp_key_service_outage_returns_503_without_invalidating_identity(client, app, monkeypatch):
    app.state.settings.mcp_issuer = "https://identity.example"
    app.state.settings.mcp_audience = "https://platform.example/mcp"
    app.state.settings.mcp_jwks_url = "https://identity.example/jwks"
    def unavailable(*args):
        raise jwt.PyJWKClientConnectionError("private internal address")
    monkeypatch.setattr(app.state.mcp_keys, "signing_key", unavailable)
    response = client.post("/mcp", headers={"Authorization": "Bearer token"}, json={"jsonrpc": "2.0"})
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert "private internal address" not in response.text


def test_oidc_linked_identity_and_mfa_boundary_survive_offload(client, actors, app):
    settings = app.state.settings
    settings.oidc_issuer = "https://identity.example"
    with app.state.db.Session() as db:
        db.add(Identity(issuer=settings.oidc_issuer, subject="admin-subject", user_id=actors["admin"]["id"]))
        db.commit()
    async def exchange(request):
        return {"userinfo": {"iss": settings.oidc_issuer, "sub": "admin-subject"}}
    app.state.oauth = SimpleNamespace(enterprise=SimpleNamespace(authorize_access_token=exchange))
    response = client.get("/api/v1/auth/oidc/callback", follow_redirects=False)
    assert response.status_code == 307 and "dp_session=" in response.headers["set-cookie"]
    with app.state.db.Session() as db:
        sessions = db.scalars(select(LoginSession).where(LoginSession.user_id == actors["admin"]["id"])).all()
        assert len(sessions) == 2 and all(not session.mfa for session in sessions)


def test_stats_use_scalar_aggregation_and_preserve_space_isolation(client, actors, app, monkeypatch):
    owner, reader, admin = (actors[name] for name in ("owner", "reader", "admin"))
    _, version = create_dataset(client, owner)
    upload(client, owner, version)
    publish(client, owner, admin, version)
    approve(client, owner, reader, version)
    _, hidden = create_dataset(client, reader)
    upload(client, reader, hidden)
    original = app.state.db.Session.class_.scalars
    def reject_entity_loading(session, statement, *args, **kwargs):
        names = {description.get("name") for description in statement.column_descriptions}
        assert not names.intersection({"Dataset", "Version", "Asset", "Application", "Job"})
        return original(session, statement, *args, **kwargs)
    monkeypatch.setattr(app.state.db.Session.class_, "scalars", reject_entity_loading)
    response = client.get("/api/v1/stats", headers=owner["headers"])
    assert response.status_code == 200
    values = response.json()
    assert values["datasets"] == values["versions"] == values["files"] == values["published"] == 1
    assert values["applications"] == 1 and values["approval_rate"] == 100 and values["pending_applications"] == 0
    assert client.get("/api/v1/stats", headers=owner["headers"], params={"space_id": reader["space_id"]}).status_code == 403


def test_runtime_status_is_privileged_and_process_scoped(client, actors):
    assert client.get("/api/v1/admin/runtime-status").status_code == 401
    assert client.get("/api/v1/admin/runtime-status", headers=actors["owner"]["headers"]).status_code == 403
    response = client.get("/api/v1/admin/runtime-status", headers=actors["admin"]["headers"])
    assert response.status_code == 200
    assert response.json()["scope"] == "current_api_process"
    assert response.json()["threads"]["limit"] == 40
    assert "database_url" not in response.text and "secret" not in response.text


@pytest.mark.parametrize("path", ["/mcp", "/api/v1/auth/login"])
def test_dripping_request_body_has_total_deadline_and_liveness_survives(app, path):
    app.state.settings.request_body_timeout_seconds = .1
    async def scenario():
        async def drip():
            for part in (b'{', b'"x"', b':', b'1', b'}'):
                await asyncio.sleep(.035)
                yield part
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
            response = await session.post(path, headers={"Content-Type": "application/json"}, content=drip())
            assert response.status_code == 408
            assert (await session.get("/health/live")).status_code == 200
    asyncio.run(scenario())


def test_upload_receive_timeout_does_not_persist_partial_bytes(client, actors, app):
    owner = actors["owner"]
    _, version = create_dataset(client, owner)
    data = b"a,b\n1,2\n"
    result = client.post(f"/api/v1/versions/{version['id']}/uploads", headers=owner["headers"], json={
        "filename": "slow.csv", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "idempotency_key": "body-deadline"})
    assert result.status_code == 201
    upload_id = result.json()["id"]
    app.state.settings.upload_receive_timeout_seconds = .1
    async def scenario():
        async def slow():
            yield data[:2]
            await asyncio.sleep(.2)
            yield data[2:]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
            return await session.put(f"/api/v1/uploads/{upload_id}/parts/1", headers=owner["headers"], content=slow())
    assert asyncio.run(scenario()).status_code == 408
    with app.state.db.Session() as db:
        assert db.get(Upload, upload_id).parts == {}


@pytest.mark.parametrize("task_name", ["dataset.run_job", "dataset.sweep"])
def test_worker_storage_initialization_failure_still_disposes_engine(app, monkeypatch, task_name):
    from app import tasks
    disposed = []
    monkeypatch.setattr(tasks, "_celery", None)
    monkeypatch.setattr(tasks, "get_settings", lambda: app.state.settings)
    monkeypatch.setattr(tasks, "Database", lambda *args: SimpleNamespace(engine=SimpleNamespace(dispose=lambda: disposed.append(True))))
    def fail(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(tasks, "make_storage", fail)
    celery = tasks.celery_app(app.state.settings)
    with pytest.raises(OSError):
        celery.tasks[task_name].run(*(["job"] if task_name == "dataset.run_job" else []))
    assert disposed == [True]


def test_storage_cleanup_failure_still_disposes_worker_engine(app, monkeypatch):
    from app import tasks
    disposed, closed = [], []
    def close():
        closed.append(True)
        raise OSError("close failed")
    monkeypatch.setattr(tasks, "_celery", None)
    monkeypatch.setattr(tasks, "get_settings", lambda: app.state.settings)
    monkeypatch.setattr(tasks, "Database", lambda *args: SimpleNamespace(engine=SimpleNamespace(dispose=lambda: disposed.append(True))))
    monkeypatch.setattr(tasks, "make_storage", lambda *args: SimpleNamespace(close=close))
    monkeypatch.setattr(tasks, "run_job", lambda *args: None)
    celery = tasks.celery_app(app.state.settings)
    with pytest.raises(OSError):
        celery.tasks["dataset.run_job"].run("job")
    assert closed == disposed == [True]
