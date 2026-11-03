"""请求保护：累计请求体、按有效身份限流，以及不记录内容或凭据的结构日志。"""
import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from uuid import uuid4

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.engine import make_url
from starlette.datastructures import MutableHeaders

from .db import now
from .models import ImportBatch, LoginSession, ServiceCredential, User
from .security import token_hash

logger = logging.getLogger("platform.requests")
PART_PATH = re.compile(r"^/api/v1/uploads/[a-f0-9]{32}/parts/[0-9]+$")
TRANSFER_PATH = re.compile(
    r'^/api/v1/(?:versions/[a-f0-9]{32}/uploads|uploads/[a-f0-9]{32}(?:/parts/[0-9]+|/complete)?|'
    r'imports(?:/[a-f0-9]{32}(?:/items(?:/[a-f0-9]{32}/(?:upload|retry-scan))?|/finalize)?)?)$')
RATE_SCRIPT = """
local n=redis.call('INCR',KEYS[1]);
if n==1 then redis.call('EXPIRE',KEYS[1],60) end;
return {n,redis.call('TTL',KEYS[1])}
"""


def rate_credentials(request, settings):
    """验证令牌签名后再使用其主体限流；不信任客户端随意填写的 user ID。"""
    authorization = request.headers.get('authorization', '')
    token = authorization.removeprefix('Bearer ') if authorization.startswith('Bearer ') else request.cookies.get('dp_session')
    if not token:
        return None
    if token.startswith(('dp_svc_', 'dp_ing_')):
        return token, None
    try:
        claims = jwt.decode(token, settings.jwt_signing_key, algorithms=['HS256'],
            audience='dataset-platform', issuer='dataset-platform', options={'require': ['exp', 'sub', 'jti']})
        if not all(isinstance(claims.get(k), str) and re.fullmatch(r'[a-f0-9]{32}', claims[k]) for k in ('sub', 'jti')):
            return None
        return token, claims
    except jwt.PyJWTError:
        return None


def rate_subject(db, token, claims):
    """纯 SQL，不修改业务认证 state，也不把 ORM 对象传出会话。"""
    if claims is not None:
        session = db.get(LoginSession, claims['jti'])
        if not (session and not session.revoked and session.expires_at > now() and session.user_id == claims['sub']):
            return None
        user = db.get(User, session.user_id)
    else:
        model = ImportBatch if token.startswith('dp_ing_') else ServiceCredential
        credential = db.scalar(select(model).where(model.token_hash == token_hash(token)))
        if not (credential and not credential.revoked and credential.expires_at > now()):
            return None
        user = db.get(User, credential.user_id)
    return user.id if user and user.active and user.verified else None


async def verified_rate_subject(request, settings):
    from .db import run_sql
    credentials = rate_credentials(request, settings)
    if credentials is None:
        return None
    try:
        return await run_sql(request, rate_subject, *credentials)
    except Exception as exc:
        # 公共路由可继续采用 IP 预算；需要身份的业务路由仍然独立验证且失败关闭。
        logger.warning(json.dumps({'event': 'rate_identity_unavailable', 'exception': type(exc).__name__}))
        return None


class RateLimiter:
    def __init__(self, settings, database, redis=None):
        self.settings, self.database, self.redis = settings, database, redis
        database_name = make_url(settings.database_url).database or "memory"
        self.namespace = "dp:rate:" + hashlib.sha256(database_name.encode()).hexdigest()[:12]
        self.buckets = OrderedDict()
        self.lock = threading.Lock()

    async def close(self):
        if self.redis is not None:
            await self.redis.aclose()

    async def check(self, request):
        auth_request = request.url.path.startswith("/api/v1/auth/") and request.method == "POST"
        subject = None
        if not auth_request and (request.headers.get("authorization") or request.cookies.get("dp_session")):
            subject = await verified_rate_subject(request, self.settings)
        principal = ("user:" + subject if subject else
                     "ip:" + (request.client.host if request.client else "unknown"))
        # 仅有效身份的明确传输路由进入单独预算；伪造身份/相似 URL 仍用匿名 API 额度。
        bucket = ('auth' if auth_request else 'transfer' if subject and TRANSFER_PATH.fullmatch(request.url.path)
                  else 'api')
        request.state.rate_bucket = bucket
        key = f"{self.namespace}:{bucket}:{principal}"
        limit = {'auth': self.settings.login_limit_per_minute, 'transfer': self.settings.transfer_limit_per_minute,
                 'api': self.settings.rate_limit_per_minute}[bucket]
        if self.redis is not None:
            count, remaining = await self.redis.eval(RATE_SCRIPT, 1, key)
            return count <= limit, max(1, remaining)
        with self.lock:
            current = time.monotonic()
            count, expires = self.buckets.get(key, (0, current + 60))
            if current >= expires:
                count, expires = 0, current + 60
            self.buckets[key] = min(count + 1, limit + 1), expires
            self.buckets.move_to_end(key)
            # 单进程回退仅用于开发和测试，限制未知地址造成的内存增长。
            while len(self.buckets) > 10000:
                self.buckets.popitem(last=False)
            return count + 1 <= limit, max(1, int(expires - current) + 1)


class RequestGuardMiddleware:
    """Pure ASGI，保持上传/下载流式传输，在实际 receive 字节数超过限额时终止。"""
    def __init__(self, app, settings, limiter, metrics=None):
        self.app, self.settings, self.limiter = app, settings, limiter
        self.metrics=metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started, status = time.monotonic(), 500
        exception_name = None
        max_body = (self.settings.part_size_bytes + 4096 if PART_PATH.fullmatch(scope["path"])
                    else 1024 * 1024)
        received = 0
        response_started = False
        body_deadline = None
        body_finished = False
        receive_timeout = (self.settings.upload_receive_timeout_seconds if PART_PATH.fullmatch(scope["path"])
                           else self.settings.request_body_timeout_seconds)

        async def bounded_receive():
            nonlocal received, body_deadline, body_finished
            # 总时限从首次读取算起，不因滴流发送下一小块而重新开始计时。
            if body_finished:
                return await receive()  # 响应流后续的 disconnect 等待不属于请求体接收。
            if body_deadline is None:
                body_deadline = time.monotonic() + receive_timeout
            try:
                async with asyncio.timeout(max(0, body_deadline - time.monotonic())):
                    message = await receive()
            except TimeoutError:
                raise HTTPException(408, "接收请求数据超时，请重试") from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body:
                    raise HTTPException(413, "请求超过大小限制")
                body_finished = not message.get("more_body", False)
            return message

        async def guarded_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                if scope["path"].startswith("/api/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        async def reject(error, message, code, headers=None):
            await JSONResponse({"error": error, "message": message}, code, headers=headers)(
                scope, receive, guarded_send)

        try:
            try:
                length = int(request.headers.get("content-length", "0"))
                if length < 0:
                    raise ValueError
            except ValueError:
                await reject("invalid_request", "无效请求长度", 400)
                return
            if length > max_body:
                await reject("too_large", "请求超过大小限制", 413)
                return
            if not scope["path"].startswith("/health/") and request.method != "OPTIONS":
                try:
                    allowed, remaining = await self.limiter.check(request)
                except Exception as exc:
                    exception_name = type(exc).__name__
                    await reject("dependency_unavailable", "服务暂不可用", 503)
                    return
                if not allowed:
                    category = scope['state'].get('rate_bucket')
                    message = (f'上传请求达到当前速率限制，请等待 {remaining} 秒后续传' if category == 'transfer' else
                               f'账号操作过于频繁，请等待 {remaining} 秒后重试' if category == 'auth' else
                               f'操作过于频繁，请等待 {remaining} 秒后重试')
                    await reject("rate_limited", message, 429,
                                 headers={"Retry-After": str(remaining)})
                    return
            await self.app(scope, bounded_receive, guarded_send)
        except Exception as exc:
            exception_name = type(exc).__name__
            from .diagnostics import report
            report(logger,'http_error',exc,request_id=request_id,method=scope['method'],path=scope['path'])
            if not response_started:
                await reject('internal', '操作未完成，请稍后重试', 500)
            else:
                # 不能发送第二个响应头，安全中断流；不把原始 SQL/连接异常交给服务器日志。
                raise RuntimeError('Response stream interrupted') from None
        finally:
            # 不记录 query、Cookie、Authorization、请求体、SQL 参数或异常原文。
            if self.metrics:self.metrics.observe(scope['path'],scope['method'],status,time.monotonic()-started)
            logger.info(json.dumps({"event": "http_request", "request_id": request_id,
                                   "method": scope["method"], "path": scope["path"], "status": status,
                                   "rate_bucket": scope['state'].get('rate_bucket'),
                                   "duration_ms": round((time.monotonic() - started) * 1000, 2),
                                   "exception": exception_name}, ensure_ascii=False))
