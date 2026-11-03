"""【学习标注：异步模型网关】排队、Redis 协调、重试等待和 HTTP 均让出事件循环。

与视觉 Worker 复用同一套 Redis Lua / 资源组 / 熔断规则。同步入口只作兼容包装，
API 的客户端在 lifespan 中复用并关闭；任何情况下不把异常正文或提示词写进计数。
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import inspect
import json
import random
import time
import uuid

import anyio
import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from . import model_gateway as g

_gateway = ContextVar("async_model_gateway", default=None)


@contextmanager
def gateway_context(gateway):
    token = _gateway.set(gateway)
    try:
        yield
    finally:
        _gateway.reset(token)


async def invoke(callback, *args):
    """异步业务直接 await；同步兼容调用只在有界线程内执行。"""
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    value = await run_in_threadpool(callback, *args)
    return await value if inspect.isawaitable(value) else value


class SyncCoordinatorAdapter:
    def __init__(self, coordinator, inline=False):
        self.coordinator, self.inline = coordinator, inline

    async def call(self, callback, *args):
        if self.inline:
            return callback(*args)  # 同步 Worker 私有事件循环，资源保持原线程。
        return await invoke(callback, *args)

    async def acquire(self, *args):
        return await self.call(self.coordinator.acquire, *args)

    async def finish(self, *args):
        return await self.call(self.coordinator.finish, *args)

    async def count(self, *args):
        return await self.call(self.coordinator.count, *args)

    async def discard(self, token):
        return await self.call(self.coordinator.discard, token)

    async def close(self):
        await self.call(self.coordinator.close)


class AsyncRedisCoordinator:
    def __init__(self, settings, client=None):
        self.settings, self.keys = settings, g.keys(settings)
        self.owned = client is None
        self.client = client if client is not None else Redis.from_url(
            settings.redis_url, socket_connect_timeout=1, socket_timeout=1,
            decode_responses=True, max_connections=32)

    async def acquire(self, token, lease_ms):
        return await self.client.eval(g.ACQUIRE, 3, *self.keys, token, self.settings.model_max_inflight,
                                      self.settings.model_breaker_failures, lease_ms)

    async def finish(self, token, epoch, outcome):
        return await self.client.eval(g.FINISH, 3, *self.keys, token, epoch, outcome,
                                      self.settings.model_breaker_failures,
                                      int(self.settings.model_breaker_seconds * 1000))

    async def count(self, name):
        async with self.client.pipeline() as pipe:
            pipe.hincrby(self.keys[2], name, 1)
            pipe.expire(self.keys[2], 86400)
            await pipe.execute()

    async def discard(self, token):
        await self.client.eval(g.DISCARD, 3, *self.keys, token)

    async def close(self):
        if self.owned:
            await self.client.aclose()


class ModelGateway:
    """每个 API 进程一个 HTTP 连接池和 Redis 连接池，不跨事件循环共享。"""
    def __init__(self, settings):
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=settings.model_max_inflight,
                                max_keepalive_connections=settings.model_max_inflight))
        self.redis = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1,
            decode_responses=True, max_connections=32) if (
                settings.environment == "production" or settings.task_mode == "celery") else None

    async def close(self):
        try:
            await self.client.aclose()
        finally:
            if self.redis is not None:
                await self.redis.aclose()


async def async_completion(settings, payload, remaining, response_bytes, time_reason="time_budget",
                           size_reason="model_response_budget", before_attempt=None, coordinator=None):
    """总预算包含排队、鉴权、推理和退避；取消释放租约，不计为模型故障。"""
    deadline = time.monotonic() + remaining
    before_attempt = before_attempt or g._authorization.get()
    gateway = _gateway.get()
    gate = coordinator or (AsyncRedisCoordinator(settings, gateway.redis if gateway else None)
        if settings.environment == "production" or settings.task_mode == "celery"
        else SyncCoordinatorAdapter(g.LocalCoordinator(settings)))
    attempts, token, epoch = 0, None, None
    try:
        while attempts < settings.model_retry_attempts:
            queue_until = min(deadline, time.monotonic() + settings.model_queue_seconds)
            token, epoch = uuid.uuid4().hex, None
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise g.ModelFailure(time_reason, attempts)
                state, acquired_epoch = await gate.acquire(token, int((left + 10) * 1000))
                if state == 2:
                    raise g.ModelFailure("model_circuit_open", attempts)
                if state == 0:
                    epoch = acquired_epoch
                    break
                if time.monotonic() >= queue_until:
                    await gate.count("busy")
                    raise g.ModelFailure("model_busy", attempts)
                await asyncio.sleep(min(.05, max(.001, queue_until - time.monotonic())))
            outcome, retry, wait = "rejected", False, 0
            try:
                if before_attempt:
                    await invoke(before_attempt)
                if deadline <= time.monotonic():
                    raise g.ModelFailure(time_reason, attempts)
                attempts += 1
                raw = await g._request(settings, payload, deadline, response_bytes, time_reason, size_reason,
                                       client=gateway.client if gateway else None)
                if time.monotonic() >= deadline:
                    raise g.ModelFailure(time_reason, attempts)
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    outcome = "failed"
                    raise g.ModelFailure("invalid_model_response", attempts) from None
                if not isinstance(body, dict):
                    outcome = "failed"
                    raise g.ModelFailure("invalid_model_response", attempts)
                outcome = "succeeded"
                return body
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except httpx.HTTPStatusError as exc:
                retry = exc.response.status_code in {429, 502, 503, 504}
                outcome = "failed" if retry else "rejected"
                if not retry:
                    raise g.ModelFailure("model_rejected", attempts) from None
                wait = g.retry_after_seconds(exc.response.headers.get("Retry-After", ""))
            except (httpx.TransportError, OSError) as exc:
                outcome, retry = "failed", True
                if isinstance(exc, httpx.TimeoutException):
                    await gate.count("timeout")
            except g.ModelFailure as exc:
                if exc.reason == time_reason:
                    outcome = "failed"
                    await gate.count("timeout")
                raise g.ModelFailure(exc.reason, attempts) from None
            finally:
                with anyio.CancelScope(shield=True):
                    try:
                        await gate.finish(token, epoch, outcome)
                    except RedisError:
                        if outcome != 'cancelled':
                            raise
                        # 断连时依靠 TTL 回收；保留 CancelledError，不能使超时任务继续执行。
                token, epoch = None, None
            if retry and attempts < settings.model_retry_attempts:
                delay = max(wait, settings.model_retry_base_seconds * 2 ** (attempts - 1) * random.uniform(.8, 1.2))
                if deadline - time.monotonic() <= delay:
                    raise g.ModelFailure(time_reason, attempts)
                await gate.count("retries")
                await asyncio.sleep(delay)
            else:
                raise g.ModelFailure("model_unavailable", attempts)
        raise g.ModelFailure("model_unavailable", attempts)
    except RedisError:
        raise g.ModelFailure("model_coordination_unavailable", attempts) from None
    finally:
        with anyio.CancelScope(shield=True):
            try:
                if token is not None:
                    await gate.discard(token)
            except RedisError:
                # 协调服务断线时租约按 TTL 回收，不能用 Redis 异常覆盖安全错误码或取消。
                pass
            finally:
                await gate.close()
