"""【学习标注：模型故障隔离】跨 API/Worker 的并发租约、熔断和有界重试。

Redis 脚本把检查与修改作为一次原子操作。并发按服务共享，熔断按服务+模型区分。
只保存计数和随机租约，不保存地址、密钥、提示词、图片或模型回答。
生产/Celery 部署协调失败即停止模型调用；不影响不依赖模型的业务工具。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import asyncio
import hashlib
import math
import threading
import time
from urllib.parse import urlsplit

import httpx
from redis import Redis, RedisError
from sqlalchemy.engine import make_url


_authorization = ContextVar('model_authorization', default=None)


@contextmanager
def authorization_guard(callback):
    token = _authorization.set(callback)
    try: yield
    finally: _authorization.reset(token)


class ModelFailure(Exception):
    def __init__(self, reason, attempts=0):
        self.reason, self.attempts = reason, attempts
        super().__init__(reason)


ACQUIRE = """
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
local failures=tonumber(redis.call('HGET',KEYS[2],'failures') or 0)
local epoch=tonumber(redis.call('HGET',KEYS[2],'epoch') or 0)
if tonumber(redis.call('HGET',KEYS[2],'open_until') or 0)>now or
   (failures>=tonumber(ARGV[3]) and tonumber(redis.call('HGET',KEYS[2],'probe_until') or 0)>now) then
  redis.call('HINCRBY',KEYS[3],'circuit_open',1); redis.call('EXPIRE',KEYS[3],86400); return {2,epoch}
end
if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[2]) then return {1,epoch} end
if failures>=tonumber(ARGV[3]) then
  redis.call('HSET',KEYS[2],'probe',ARGV[1],'probe_until',now+tonumber(ARGV[4]))
end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[4]),ARGV[1])
local last=redis.call('ZREVRANGE',KEYS[1],0,0,'WITHSCORES')
redis.call('PEXPIRE',KEYS[1],math.ceil(tonumber(last[2])-now)+1000)
redis.call('EXPIRE',KEYS[2],86400)
redis.call('HINCRBY',KEYS[3],'attempts',1); redis.call('EXPIRE',KEYS[3],86400)
return {0,epoch}
"""
FINISH = """
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
local expiry=tonumber(redis.call('ZSCORE',KEYS[1],ARGV[1]) or 0)
redis.call('ZREM',KEYS[1],ARGV[1])
redis.call('HINCRBY',KEYS[3],ARGV[3],1); redis.call('EXPIRE',KEYS[3],86400)
if expiry<=now or tonumber(redis.call('HGET',KEYS[2],'epoch') or 0)~=tonumber(ARGV[2]) then return 0 end
if ARGV[3]=='succeeded' then
  redis.call('HSET',KEYS[2],'failures',0,'open_until',0)
  redis.call('HDEL',KEYS[2],'probe','probe_until')
elseif ARGV[3]=='failed' then
  local failures=redis.call('HINCRBY',KEYS[2],'failures',1)
  if failures>=tonumber(ARGV[4]) then
    redis.call('HSET',KEYS[2],'open_until',now+tonumber(ARGV[5]))
    redis.call('HINCRBY',KEYS[2],'epoch',1)
    redis.call('HDEL',KEYS[2],'probe','probe_until')
  end
else
  if redis.call('HGET',KEYS[2],'probe')==ARGV[1] then redis.call('HDEL',KEYS[2],'probe','probe_until') end
end
redis.call('EXPIRE',KEYS[2],86400); return 1
"""


DISCARD = """
local removed=redis.call('ZREM',KEYS[1],ARGV[1])
if redis.call('HGET',KEYS[2],'probe')==ARGV[1] then redis.call('HDEL',KEYS[2],'probe','probe_until') end
if removed>0 then redis.call('HINCRBY',KEYS[3],'cancelled',1); redis.call('EXPIRE',KEYS[3],86400) end
return removed
"""


def keys(settings):
    # 显式资源组可把宿主机/容器地址别名或同一 GPU 的多个服务并入一个额度。
    url = urlsplit(settings.model_url)
    endpoint = settings.model_resource_group or f'{url.scheme.lower()}://{url.netloc.lower()}{url.path.rstrip("/")}'
    database = make_url(settings.database_url).database or 'memory'
    group = hashlib.sha256((database + '\n' + endpoint).encode()).hexdigest()[:24]
    model = hashlib.sha256(settings.model_name.encode()).hexdigest()[:16]
    # Redis Cluster 下脚本的三个 key 使用相同哈希标签。
    base = 'dp:model:{' + group + '}:'
    return base + 'slots', base + model + ':circuit', base + model + ':counts'


class RedisCoordinator:
    def __init__(self, settings):
        self.settings, self.keys = settings, keys(settings)
        self.client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1,
                                     decode_responses=True)

    def acquire(self, token, lease_ms):
        return self.client.eval(ACQUIRE, 3, *self.keys, token, self.settings.model_max_inflight,
                                self.settings.model_breaker_failures, lease_ms)

    def finish(self, token, epoch, outcome):
        return self.client.eval(FINISH, 3, *self.keys, token, epoch, outcome,
                                self.settings.model_breaker_failures, int(self.settings.model_breaker_seconds * 1000))

    def count(self, name):
        pipe = self.client.pipeline()
        pipe.hincrby(self.keys[2], name, 1); pipe.expire(self.keys[2], 86400); pipe.execute()

    def snapshot(self):
        stamp = self.client.time(); now = stamp[0] * 1000 + stamp[1] // 1000
        state = self.client.hgetall(self.keys[1])
        return {'coordination': 'redis', 'inflight': self.client.zcount(self.keys[0], now + 1, '+inf'),
                'open_seconds': max(0, round((int(state.get('open_until', 0)) - now) / 1000, 2)),
                'consecutive_failures': int(state.get('failures', 0)),
                'counts': {k: int(v) for k, v in self.client.hgetall(self.keys[2]).items()}}

    def close(self):
        self.client.close()

    def discard(self, token):
        return self.client.eval(DISCARD, 3, *self.keys, token)


_local_lock, _local_slots, _local_circuits, _local_counts = threading.Lock(), {}, {}, {}


class LocalCoordinator:
    """仅 inline/manual 开发测试使用；实际多人部署必须使用 Redis。"""
    def __init__(self, settings):
        self.settings, self.keys = settings, keys(settings)

    def acquire(self, token, lease_ms):
        with _local_lock:
            now = time.monotonic() * 1000
            slots = _local_slots.setdefault(self.keys[0], {})
            for stale in [k for k, until in slots.items() if until <= now]: slots.pop(stale)
            c = _local_circuits.setdefault(self.keys[1], {'failures': 0, 'epoch': 0})
            if c.get('open_until', 0) > now or (c['failures'] >= self.settings.model_breaker_failures
                                               and c.get('probe_until', 0) > now):
                self._count('circuit_open'); return [2, c['epoch']]
            if len(slots) >= self.settings.model_max_inflight: return [1, c['epoch']]
            if c['failures'] >= self.settings.model_breaker_failures:
                c.update(probe=token, probe_until=now + lease_ms)
            slots[token] = now + lease_ms
            self._count('attempts')
            return [0, c['epoch']]

    def _count(self, name):
        values = _local_counts.setdefault(self.keys[2], {})
        values[name] = values.get(name, 0) + 1

    def count(self, name):
        with _local_lock: self._count(name)

    def finish(self, token, epoch, outcome):
        with _local_lock:
            now = time.monotonic() * 1000
            until = _local_slots.setdefault(self.keys[0], {}).pop(token, 0)
            self._count(outcome)
            c = _local_circuits.setdefault(self.keys[1], {'failures': 0, 'epoch': 0})
            if until <= now or c['epoch'] != epoch: return
            if outcome == 'succeeded': c.update(failures=0, open_until=0, probe_until=0)
            elif outcome == 'failed':
                c['failures'] += 1
                if c['failures'] >= self.settings.model_breaker_failures:
                    c.update(open_until=now + self.settings.model_breaker_seconds * 1000,
                             epoch=c['epoch'] + 1, probe_until=0)
            elif c.get('probe') == token: c['probe_until'] = 0

    def snapshot(self):
        with _local_lock:
            now = time.monotonic() * 1000
            c = _local_circuits.get(self.keys[1], {})
            return {'coordination': 'local-development',
                    'inflight': sum(v > now for v in _local_slots.get(self.keys[0], {}).values()),
                    'open_seconds': max(0, round((c.get('open_until', 0) - now) / 1000, 2)),
                    'consecutive_failures': c.get('failures', 0),
                    'counts': dict(_local_counts.get(self.keys[2], {}))}

    def close(self): pass

    def discard(self, token):
        with _local_lock:
            if _local_slots.setdefault(self.keys[0], {}).pop(token, None) is not None:
                self._count('cancelled')
            circuit = _local_circuits.get(self.keys[1], {})
            if circuit.get('probe') == token:
                circuit.pop('probe', None)
                circuit['probe_until'] = 0


def coordinator(settings):
    return RedisCoordinator(settings) if settings.environment == 'production' or settings.task_mode == 'celery' \
        else LocalCoordinator(settings)


def status(settings):
    gate = coordinator(settings)
    try:
        return {**gate.snapshot(), 'max_inflight': settings.model_max_inflight,
                'queue_seconds': settings.model_queue_seconds, 'retry_attempts': settings.model_retry_attempts,
                'state_is_not_live_probe': True}
    except RedisError:
        return {'coordination': 'unavailable', 'state_is_not_live_probe': True}
    finally: gate.close()


def retry_after_seconds(value):
    """支持秒数和 HTTP 日期；等待超出剩余预算时停止，而非提前重试。"""
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            return max(0, (stamp - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError): return 0


async def _request(settings, payload, deadline, response_bytes, time_reason, size_reason, client=None):
    """总期限包括慢速响应；API 复用 lifespan 客户端，同步工人调用创建独立客户端。"""
    left = deadline - time.monotonic()
    if left <= 0: raise ModelFailure(time_reason)
    async def read(active):
        async with active.stream('POST', settings.model_url.rstrip('/') + '/chat/completions', json=payload,
                timeout=httpx.Timeout(left, connect=min(3, left), pool=min(1, left)),
                headers={'Authorization': 'Bearer ' + settings.model_api_key} if settings.model_api_key else {}) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > response_bytes: raise ModelFailure(size_reason)
            return raw
    try:
        async with asyncio.timeout(left):
            if client is not None:
                return await read(client)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as active:
                return await read(active)
    except TimeoutError:
        raise ModelFailure(time_reason) from None


def completion(settings, payload, remaining, response_bytes, time_reason='time_budget',
               size_reason='model_response_budget', before_attempt=None):
    """【学习标注：同步兼容入口】Celery 工人调用；API 直接 await async_completion。

    同一重试实现，避免两条链路的授权、预算、熔断和错误分类逐渐不一致。
    此包装只可在同步代码调用，不能在 API 事件循环内使用 asyncio.run。
    """
    from .async_model_gateway import async_completion, SyncCoordinatorAdapter
    import inspect
    callback = before_attempt or _authorization.get()
    async def validate():
        if callback:
            value = callback()  # 与创建同步会话的调用方保持同一线程。
            if inspect.isawaitable(value):
                await value
    return asyncio.run(async_completion(settings, payload, remaining, response_bytes, time_reason, size_reason,
        before_attempt=validate, coordinator=SyncCoordinatorAdapter(coordinator(settings), inline=True)))
