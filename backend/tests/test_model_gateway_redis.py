"""真实 Redis/多进程测试；使用随机模型资源组，不修改业务数据和现有模型状态。"""
import multiprocessing
import os
from pathlib import Path
import time
import uuid

import pytest

from app.config import Settings
from app.model_gateway import RedisCoordinator, keys


def admission_process(config, barrier, output):
    cfg = Settings(_env_file=None, **config)
    gate = RedisCoordinator(cfg)
    try:
        barrier.wait(timeout=15)
        token = uuid.uuid4().hex
        state, epoch = gate.acquire(token, 2000)
        if state == 0:
            time.sleep(.3); gate.finish(token, epoch, 'succeeded')
        output.put(state)
    finally: gate.close()


@pytest.fixture
def redis_cfg():
    if os.environ.get('DP_TEST_REDIS') != '1': pytest.skip('DP_TEST_REDIS=1 才运行真实 Redis 隔离测试')
    path = Path('.env.integration.local')
    assert path.is_file()
    cfg = Settings(_env_file=path, environment='test', task_mode='celery',
                   model_url='http://127.0.0.1:1234/v1', model_name='isolated-fault-test',
                   model_resource_group='gateway-test-' + uuid.uuid4().hex, model_max_inflight=2,
                   model_breaker_failures=1, model_breaker_seconds=.1)
    gate = RedisCoordinator(cfg)
    gate.client.ping()
    yield cfg
    # 仅删除这个随机隔离资源组的三个明确 key。
    gate.client.delete(*keys(cfg)); gate.close()


def test_real_redis_atomic_admission_across_four_processes(redis_cfg):
    context = multiprocessing.get_context('spawn')
    barrier, output = context.Barrier(4), context.Queue()
    processes = [context.Process(target=admission_process, args=(redis_cfg.model_dump(), barrier, output)) for _ in range(4)]
    try:
        for process in processes: process.start()
        states = [output.get(timeout=20) for _ in processes]
        for process in processes: process.join(15); assert process.exitcode == 0
        assert sorted(states) == [0, 0, 1, 1]
        gate = RedisCoordinator(redis_cfg)
        try:
            assert gate.snapshot()['inflight'] == 0
            assert gate.snapshot()['counts']['attempts'] == 2
        finally: gate.close()
    finally:
        for process in processes:
            if process.is_alive(): process.terminate(); process.join(3)
        output.close()


def test_real_redis_probe_is_unique_and_old_success_cannot_close_breaker(redis_cfg):
    a, b = RedisCoordinator(redis_cfg), RedisCoordinator(redis_cfg)
    try:
        assert a.acquire('old', 1000) == [0, 0]
        assert b.acquire('bad', 1000) == [0, 0]
        b.finish('bad', 0, 'failed'); a.finish('old', 0, 'succeeded')
        assert a.acquire('denied', 1000)[0] == 2
        time.sleep(.12)
        state, epoch = a.acquire('probe', 1000)
        assert state == 0 and b.acquire('other-probe', 1000)[0] == 2
        a.finish('probe', epoch, 'succeeded')
        state, epoch = b.acquire('recovered', 1000)
        assert state == 0; b.finish('recovered', epoch, 'succeeded')
        assert a.snapshot()['inflight'] == 0 and a.snapshot()['consecutive_failures'] == 0
    finally: a.close(); b.close()


def test_short_lease_does_not_expire_an_existing_long_lease(redis_cfg):
    gate = RedisCoordinator(redis_cfg)
    try:
        assert gate.acquire('vision-long', 60000)[0] == 0
        assert gate.acquire('chat-short', 1000)[0] == 0
        assert gate.client.pttl(gate.keys[0]) > 59000
        gate.finish('vision-long', 0, 'succeeded'); gate.finish('chat-short', 0, 'succeeded')
    finally: gate.close()


def test_native_async_gateway_shares_worker_capacity_and_cancel_returns_lease(redis_cfg, monkeypatch):
    import asyncio
    import httpx
    from app import model_gateway as gateway_module
    from app.async_model_gateway import async_completion
    async def exercise():
        cfg = redis_cfg.model_copy(update={'model_max_inflight': 1, 'model_queue_seconds': 0})
        worker = RedisCoordinator(cfg)
        arrived, closed = asyncio.Event(), asyncio.Event()
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                arrived.set()
                await asyncio.sleep(60)
                yield b'{}'
            async def aclose(self):
                closed.set()
        original = httpx.AsyncClient
        sends = []
        def respond(request):
            sends.append(1)
            return httpx.Response(200, stream=Waiting())
        monkeypatch.setattr(gateway_module.httpx, 'AsyncClient', lambda **kw:
            original(transport=httpx.MockTransport(respond), **kw))
        task = None
        try:
            state, epoch = worker.acquire('vision-worker', 10000)
            assert state == 0
            with pytest.raises(gateway_module.ModelFailure, match='model_busy'):
                await async_completion(cfg, {}, 1, 4096)
            assert not sends and worker.snapshot()['inflight'] == 1
            worker.finish('vision-worker', epoch, 'succeeded')
            task = asyncio.create_task(async_completion(cfg, {}, 5, 4096))
            await asyncio.wait_for(arrived.wait(), 2)
            assert worker.snapshot()['inflight'] == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            state = worker.snapshot()
            assert closed.is_set() and state['inflight'] == 0
            assert state['consecutive_failures'] == 0 and state['counts']['cancelled'] == 1
        finally:
            if task is not None:
                task.cancel()
            worker.close()
    asyncio.run(exercise())


def test_unknown_async_acquire_result_releases_half_open_probe(redis_cfg):
    import asyncio
    from app.async_model_gateway import AsyncRedisCoordinator, async_completion
    async def exercise():
        worker = RedisCoordinator(redis_cfg)
        class Interrupted(AsyncRedisCoordinator):
            async def acquire(self, *args):
                state = await super().acquire(*args)
                assert state[0] == 0
                raise asyncio.CancelledError()  # 服务端已领取，客户端尚未取得响应。
        try:
            _, epoch = worker.acquire('open-breaker', 1000)
            worker.finish('open-breaker', epoch, 'failed')
            await asyncio.sleep(.12)
            with pytest.raises(asyncio.CancelledError):
                await async_completion(redis_cfg, {}, 1, 4096, coordinator=Interrupted(redis_cfg))
            assert worker.snapshot()['inflight'] == 0
            state, epoch = worker.acquire('recovery-probe', 1000)
            assert state == 0
            worker.finish('recovery-probe', epoch, 'succeeded')
        finally:
            worker.close()
    asyncio.run(exercise())
