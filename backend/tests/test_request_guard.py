"""多人共享出口、身份撤销、Redis 故障与流式请求体边界的回归。"""
import asyncio
import json
import logging
from datetime import timedelta

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.db import now
from app.models import LoginSession, ServiceCredential, User
from app.security import token_hash


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_actual_body_is_bounded_even_without_correct_content_length(client, declared_length):
    body = json.dumps({"email": "unused@example.test", "password": "x" * (1024**2 + 100)}).encode()
    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    request = client.build_request("POST", "/api/v1/auth/login", headers=headers,
                                   content=iter([body[:600000], body[600000:]]))
    if declared_length is None:
        assert request.headers.get("content-length") is None
    response = client.send(request)
    assert response.status_code == 413
    assert response.json()["error"] == "too_large"
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("length", ["-1", "bad", "2097152"])
def test_early_rejections_have_request_id(client, length):
    response = client.post("/api/v1/auth/login", content=b"{}", headers={"Content-Length": length})
    assert response.status_code == (413 if length == "2097152" else 400)
    assert len(response.headers["x-request-id"]) == 32
    assert response.headers["cache-control"] == "no-store"


def test_cross_origin_frontend_can_read_early_rejections(app):
    app.state.settings.cors_origins = ['https://portal.example.test']
    # 中间件在首次请求时构建，这里同步其启动参数。
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == 'CORSMiddleware':
            middleware.kwargs['allow_origins'] = app.state.settings.cors_origins
    with TestClient(app) as client:
        response = client.post('/api/v1/auth/login', content=b'{}',
            headers={'Content-Length': '2097152', 'Origin': 'https://portal.example.test'})
    assert response.status_code == 413
    assert response.headers['access-control-allow-origin'] == 'https://portal.example.test'
    assert 'X-Request-ID' in response.headers['access-control-expose-headers']


def test_shared_nat_reviewers_have_independent_budgets(client, actors, app):
    app.state.settings.rate_limit_per_minute = 1
    # 用户初始化已调用空间接口，此处开始独立预算窗口。
    app.state.rate_limiter.buckets.clear()
    assert client.get("/api/v1/config", headers=actors["reader"]["headers"]).status_code == 200
    assert client.get("/api/v1/config", headers=actors["stranger"]["headers"]).status_code == 200
    limited = client.get("/api/v1/config", headers=actors["reader"]["headers"])
    assert limited.status_code == 429 and limited.headers["retry-after"]
    assert client.get("/api/v1/config").status_code == 200
    assert client.get("/api/v1/config").status_code == 429


def test_sessions_and_service_credentials_share_owner_budget(client, actors, app):
    owner = actors["owner"]
    tokens = ["dp_svc_test-request-budget-a", "dp_svc_test-request-budget-b"]
    with app.state.db.Session() as db:
        for token in tokens:
            db.add(ServiceCredential(user_id=owner["id"], name="test-budget", token_hash=token_hash(token),
                                     scopes=["catalog:read"], expires_at=now() + timedelta(hours=1)))
        db.commit()
    app.state.settings.rate_limit_per_minute = 2
    app.state.rate_limiter.buckets.clear()
    assert client.get("/api/v1/config", headers=owner["headers"]).status_code == 200
    assert client.get("/api/v1/config", headers={"Authorization": "Bearer " + tokens[0]}).status_code == 200
    assert client.get("/api/v1/config", headers={"Authorization": "Bearer " + tokens[1]}).status_code == 429


@pytest.mark.parametrize("invalidity", ["revoked-session", "disabled-user", "unverified-user", "forged", "unknown-session"])
def test_invalid_identity_cannot_create_new_budget_or_change_public_semantics(client, actors, app, invalidity):
    actor = actors["reader"]
    token = actor["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, app.state.settings.secret_key, algorithms=["HS256"],
                        audience="dataset-platform", issuer="dataset-platform")
    with app.state.db.Session() as db:
        if invalidity == "revoked-session":
            db.get(LoginSession, claims["jti"]).revoked = True
        elif invalidity in {"disabled-user", "unverified-user"}:
            user = db.get(User, actor["id"])
            if invalidity == "disabled-user":
                user.active = False
            else:
                user.verified = False
        elif invalidity == "unknown-session":
            claims["jti"] = "0" * 32
            token = jwt.encode(claims, app.state.settings.secret_key, algorithm="HS256")
        else:
            token = jwt.encode(claims, "different-signing-key-0123456789000000", algorithm="HS256")
        db.commit()
    app.state.settings.rate_limit_per_minute = 1
    app.state.rate_limiter.buckets.clear()
    # 无须登录的配置接口仍返回公开配置；无效 token 不能绕过匿名 IP 预算。
    assert client.get("/api/v1/config", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert client.get("/api/v1/config").status_code == 429


def test_login_limits_still_use_ip_even_with_existing_session(client, actors, app):
    app.state.settings.login_limit_per_minute = 1
    app.state.rate_limiter.buckets.clear()
    first = client.post("/api/v1/auth/login", headers=actors["reader"]["headers"],
                        json={"email": "unknown@example.test", "password": "unused-password"})
    assert first.status_code == 401
    assert client.post("/api/v1/auth/login", headers=actors["stranger"]["headers"],
                       json={"email": "unknown@example.test", "password": "unused-password"}).status_code == 429


def test_transfer_and_catalog_have_independent_bounded_budgets(client,actors,app):
    cfg=app.state.settings;cfg.rate_limit_per_minute=1;cfg.transfer_limit_per_minute=2
    app.state.rate_limiter.buckets.clear()
    headers=actors['owner']['headers']
    assert client.get('/api/v1/config',headers=headers).status_code==200
    assert client.get('/api/v1/config',headers=headers).status_code==429
    assert client.get('/api/v1/imports',headers=headers).status_code==200
    assert client.get('/api/v1/imports',headers=headers).status_code==200
    rejected=client.get('/api/v1/imports',headers=headers)
    assert rejected.status_code==429 and int(rejected.headers['retry-after'])>=1
    assert '续传' in rejected.json()['message']
    assert client.get('/api/v1/imports',headers=actors['stranger']['headers']).status_code==200
    assert client.get('/api/v1/config',headers=actors['stranger']['headers']).status_code==200


@pytest.mark.parametrize('route',['/api/v1/uploads/'+'0'*32,'/api/v1/imports'])
def test_invalid_identity_cannot_use_transfer_budget(client,app,route):
    app.state.settings.rate_limit_per_minute=1
    app.state.settings.transfer_limit_per_minute=1000
    app.state.rate_limiter.buckets.clear()
    headers={'Authorization':'Bearer forged-not-a-session'}
    assert client.get(route,headers=headers).status_code==401
    assert client.get(route,headers=headers).status_code==429


@pytest.mark.parametrize('route',[
    '/api/v1/imports/client-kit','/api/v1/imports/'+'0'*32+'/credential',
    '/api/v1/uploads/not-an-id/parts/1','/api/v1/imports/'+'0'*32+'/not-a-transfer-route'])
def test_similar_or_sensitive_routes_do_not_get_transfer_allowance(client,actors,app,route):
    app.state.settings.rate_limit_per_minute=1;app.state.settings.transfer_limit_per_minute=1000
    app.state.rate_limiter.buckets.clear();headers=actors['owner']['headers']
    assert client.get('/api/v1/config',headers=headers).status_code==200
    assert client.get(route,headers=headers).status_code==429


def test_redis_uses_shared_owner_transfer_key_for_cookie_and_scoped_token(client,actors,app):
    from test_lifecycle import create_dataset
    from test_imports import create_import,png
    owner=actors['owner'];_,version=create_dataset(client,owner,modality='image')
    batch=create_import(client,owner,version,png())
    calls=[]
    class Redis:
        async def eval(self,script,count,key):calls.append(key);return [1,60]
        async def aclose(self):pass
    app.state.rate_limiter.redis=Redis()
    assert client.get('/api/v1/imports/'+batch['id'],headers=owner['headers']).status_code==200
    assert client.get('/api/v1/imports/'+batch['id'],headers=batch['headers']).status_code==200
    assert calls[0]==calls[1] and ':transfer:user:' in calls[0]


def test_redis_failure_is_closed_and_does_not_break_liveness(client, app):
    class UnavailableRedis:
        async def eval(self, *args):
            raise ConnectionError("contains-private-connection-information")

        async def aclose(self):
            pass

    app.state.rate_limiter.redis = UnavailableRedis()
    response = client.get("/api/v1/config")
    assert response.status_code == 503
    assert response.json()["error"] == "dependency_unavailable"
    assert response.headers["x-request-id"]
    assert client.get("/health/live").status_code == 200


def test_redis_requests_await_concurrently_without_blocking_event_loop(app):
    class AsyncRedisProbe:
        def __init__(self):
            self.inflight = 0
            self.maximum = 0

        async def eval(self, *args):
            self.inflight += 1
            self.maximum = max(self.maximum, self.inflight)
            await asyncio.sleep(0.02)
            self.inflight -= 1
            return [1, 60]

        async def aclose(self):
            pass

    probe = AsyncRedisProbe()
    app.state.rate_limiter.redis = probe

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as session:
            results = await asyncio.gather(session.get("/api/v1/config"), session.get("/api/v1/config"))
        assert [response.status_code for response in results] == [200, 200]
        assert probe.maximum == 2

    asyncio.run(scenario())


def test_internal_errors_are_correlated_without_logging_request_secrets(app, caplog):
    @app.get("/api/v1/test-request-error")
    def fail():
        raise RuntimeError("must-not-log-this-sensitive-value")

    with caplog.at_level(logging.INFO, logger="platform.requests"):
        with TestClient(app, raise_server_exceptions=False) as session:
            response = session.get("/api/v1/test-request-error?reset=secret-query-token",
                                   headers={"Authorization": "Bearer secret-header-token",
                                            "Cookie": "unrelated=secret-cookie-token"})
    assert response.status_code == 500
    request_id = response.headers["x-request-id"]
    records = [json.loads(record.message) for record in caplog.records if record.name == "platform.requests"]
    assert any(record.get("event") == "http_error" and record["request_id"] == request_id for record in records)
    assert any(record.get("status") == 500 and record["request_id"] == request_id for record in records)
    serialized = json.dumps(records)
    for secret in ("secret-query-token", "secret-header-token", "secret-cookie-token", "must-not-log-this-sensitive-value"):
        assert secret not in serialized
