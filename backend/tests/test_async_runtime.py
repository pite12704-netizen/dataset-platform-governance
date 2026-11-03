"""异步线上边界：等待不占线程/连接，取消清理，异步/同步协调同一额度。"""
import asyncio

import time
import uuid

import anyio
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.async_model_gateway import async_completion, ModelGateway
from app.async_storage import AsyncObjectReader, download_response
from app.assistant import ChatInput, chat
from app import model_gateway as g
from app.models import AgentConversation, AgentRun


def request_for(app, actor, receive=None):
    async def empty():
        await asyncio.sleep(3600)
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/v1/agent/chat',
        'headers': [(b'authorization', actor['headers']['Authorization'].encode())],
        'app': app, 'scheme': 'http', 'server': ('testserver', 80)}, receive=receive or empty)


def test_native_agent_wait_does_not_hold_db_or_api_threads_and_cancellation_is_durable(app, client, actors):
    async def exercise():
        cfg = app.state.settings
        cfg.agent_tools_enabled = True
        cfg.model_url, cfg.model_name = 'http://127.0.0.1:1234/v1', 'controlled-provider'
        cfg.model_resource_group = uuid.uuid4().hex
        arrived, closed = asyncio.Event(), asyncio.Event()
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                arrived.set()
                await asyncio.sleep(60)
                yield b'{}'
            async def aclose(self):
                closed.set()
        gateway = app.state.model_gateway or ModelGateway(cfg)
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Waiting())))
        app.state.model_gateway = gateway
        assert app.state.async_db is not None
        request_id = uuid.uuid4().hex
        task = asyncio.create_task(chat(ChatInput(message='查找', intent='agent', request_id=request_id),
                                        request_for(app, actors['owner'])))
        try:
            await asyncio.wait_for(arrived.wait(), 2)
            assert app.state.async_db.engine.pool.checkedout() == 0
            assert app.state.db.engine.pool.checkedout() == 0
            assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0
            # 大模型仍在等待，事件循环可以持续处理其他活动。
            ticks = 0
            until = time.monotonic() + .08
            while time.monotonic() < until:
                ticks += 1
                await asyncio.sleep(.002)
            assert ticks >= 5
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            assert g.status(cfg)['inflight'] == 0
            with app.state.db.Session() as db:
                row = db.scalar(select(AgentRun).where(AgentRun.request_id == request_id))
                assert row.status == 'failed' and row.outputs['failure_reason'] == 'execution_cancelled'
                assert db.get(AgentConversation, row.conversation_id).active_run_id is None
            assert app.state.async_db.engine.pool.checkedout() == 0
        finally:
            task.cancel()
            await gateway.close()
            await app.state.async_db.engine.dispose()
    client.portal.call(exercise)


def test_browser_disconnect_cancels_agent_and_releases_conversation(app, client, actors, monkeypatch):
    async def exercise():
        arrived, disconnected = asyncio.Event(), asyncio.Event()
        cfg = app.state.settings
        cfg.agent_tools_enabled = True
        cfg.model_url, cfg.model_name = 'http://127.0.0.1:1234/v1', 'controlled-provider'
        async def waiting(*args):
            arrived.set()
            await asyncio.sleep(60)
        monkeypatch.setattr('app.agent_runtime.async_model_response', waiting)
        async def receive():
            if disconnected.is_set():
                return {'type': 'http.disconnect'}
            await asyncio.sleep(60)
        task = asyncio.create_task(chat(ChatInput(message='查找', intent='agent'),
                                        request_for(app, actors['owner'], receive)))
        try:
            await asyncio.wait_for(arrived.wait(), 2)
            disconnected.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            with app.state.db.Session() as db:
                row = db.scalar(select(AgentRun).where(AgentRun.user_id == actors['owner']['id']))
                assert row.status == 'failed'
                assert db.get(AgentConversation, row.conversation_id).active_run_id is None
        finally:
            task.cancel()
            await app.state.async_db.engine.dispose()
    client.portal.call(exercise)


def test_async_retries_revalidate_permissions_without_second_provider_request(app, monkeypatch):
    async def exercise():
        cfg = app.state.settings
        cfg.model_url, cfg.model_name = 'http://127.0.0.1:1234/v1', 'controlled-provider'
        cfg.model_resource_group = uuid.uuid4().hex
        cfg.model_retry_base_seconds = .01
        calls, checks = [], []
        original = httpx.AsyncClient
        def respond(request):
            calls.append(1)
            return httpx.Response(503)
        monkeypatch.setattr(g.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
        async def validate():
            checks.append(1)
            if len(checks) > 1:
                raise HTTPException(403, 'revoked')
        with pytest.raises(HTTPException):
            await async_completion(cfg, {}, 1, 4096, before_attempt=validate)
        assert len(calls) == 1 and len(checks) == 2
        assert g.status(cfg)['inflight'] == 0
    asyncio.run(exercise())


def test_async_s3_range_is_checked_before_response_and_signature_is_internal(app):
    async def exercise():
        cfg = app.state.settings.model_copy(update={'storage_backend': 's3'})
        class Client:
            def generate_presigned_url(self, operation, Params, ExpiresIn):
                assert operation == 'get_object' and Params['Range'] == 'bytes=1-3'
                return 'https://storage.test/bucket/object?signature=private'
        class Storage:
            bucket = 'private'
            client = Client()
        reader = AsyncObjectReader(Storage(), cfg)
        requests = []
        def respond(request):
            requests.append(request)
            assert request.headers['Range'] == 'bytes=1-3'
            return httpx.Response(206, headers={'Content-Length': '3', 'Content-Range': 'bytes 1-3/5'},
                                  stream=httpx.ByteStream(b'bcd'))
        reader.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        stream = await reader.open('object', 1, 3)
        assert b''.join([chunk async for chunk in stream.chunks()]) == b'bcd'
        await stream.close()
        assert stream.closed and len(requests) == 1
        await reader.client.aclose()
        reader.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, content=b'full-file-should-never-leak')))
        with pytest.raises(HTTPException) as error:
            await reader.open('object', 1, 3)
        assert error.value.status_code == 503 and 'signature' not in str(error.value.detail)
        await reader.close()
    asyncio.run(exercise())


def test_download_backpressure_and_send_failure_release_stream_and_capacity(app, client, actors):
    async def exercise():
        closed = []
        class Stream:
            async def chunks(self):
                yield b'data'
            async def close(self):
                closed.append(True)
        class Reader:
            async def open(self, *args):
                return Stream()
            async def close(self):
                pass
        app.state.async_storage = Reader()
        app.state.download_limiter = anyio.CapacityLimiter(1)
        request = request_for(app, actors['owner'])
        descriptor = {'key': 'hidden', 'start': 0, 'end': 3, 'headers': {}, 'status_code': 200}
        response = await download_response(request, descriptor, lambda db: None)
        with pytest.raises(HTTPException) as error:
            await download_response(request, descriptor, lambda db: None)
        assert error.value.status_code == 503
        async def receive():
            await asyncio.sleep(10)
        async def send(message):
            raise OSError('disconnected')
        with pytest.raises(Exception):
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert closed == [True] and app.state.download_limiter.borrowed_tokens == 0
    client.portal.call(exercise)


def test_multipart_admission_bounds_slow_receivers_without_locks(app, client, actors):
    import hashlib
    from test_lifecycle import create_dataset
    owner = actors['owner']
    _, version = create_dataset(client, owner)
    data = b'slow-part'
    upload = client.post('/api/v1/versions/' + version['id'] + '/uploads', headers=owner['headers'], json={
        'filename': 'slow.txt', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
        'idempotency_key': uuid.uuid4().hex}).json()
    async def exercise():
        app.state.upload_limiter = anyio.CapacityLimiter(1)
        arrived, release = asyncio.Event(), asyncio.Event()
        async def body():
            arrived.set()
            await release.wait()
            yield data
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
            task = asyncio.create_task(c.put('/api/v1/uploads/' + upload['id'] + '/parts/1',
                headers=owner['headers'], content=body()))
            try:
                await asyncio.wait_for(arrived.wait(), 2)
                assert app.state.db.engine.pool.checkedout() == app.state.async_db.engine.pool.checkedout() == 0
                second = await c.put('/api/v1/uploads/' + upload['id'] + '/parts/1',
                    headers=owner['headers'], content=data)
                assert second.status_code == 503 and second.headers['Retry-After'] == '1'
                assert (await c.get('/health/live')).status_code == 200
                state = await c.get('/api/v1/uploads/' + upload['id'], headers=owner['headers'])
                assert state.json()['parts'] == {}
                release.set()
                assert (await task).status_code == 200
                assert app.state.upload_limiter.borrowed_tokens == 0
            finally:
                release.set()
                if not task.done():
                    await task
    client.portal.call(exercise)


def test_cancel_during_prepare_waits_for_claim_and_releases_it(app, client, actors, monkeypatch):
    from app import assistant
    original = assistant.prepare_chat
    async def exercise():
        arrived, release = asyncio.Event(), asyncio.Event()
        # 将代理内实际数据库调用固定在原始函数，避免递归。
        from app.db import AsyncDatabase
        async def controlled(request, operation, *args):
            context = await AsyncDatabase.run(request.app.state.async_db, operation, *args)
            if operation is original:
                arrived.set()
                await release.wait()
            return context
        monkeypatch.setattr('app.db.run_sql', controlled)
        task = asyncio.create_task(chat(ChatInput(message='任务进度', intent='jobs'), request_for(app, actors['owner'])))
        try:
            await asyncio.wait_for(arrived.wait(), 2)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            with app.state.db.Session() as db:
                row = db.scalar(select(AgentRun).where(AgentRun.user_id == actors['owner']['id']))
                assert row.status == 'failed' and db.get(AgentConversation, row.conversation_id).active_run_id is None
        finally:
            release.set()
            if not task.done():
                await task
    client.portal.call(exercise)


def test_cancel_is_not_suppressed_when_redis_release_fails(app, monkeypatch):
    from redis.exceptions import ConnectionError
    async def exercise():
        arrived = asyncio.Event()
        cfg = app.state.settings
        cfg.model_url = 'http://127.0.0.1:1234/v1'
        class BrokenRelease:
            async def acquire(self, *args): return [0, 0]
            async def finish(self, *args): raise ConnectionError('private connection detail')
            async def discard(self, *args): pass
            async def close(self): pass
        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                arrived.set()
                await asyncio.sleep(60)
                yield b'{}'
        original = httpx.AsyncClient
        monkeypatch.setattr(g.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Waiting())), **kw))
        task = asyncio.create_task(async_completion(cfg, {}, 5, 4096, coordinator=BrokenRelease()))
        await asyncio.wait_for(arrived.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())


def test_terminal_persistence_has_separate_deadline_and_safe_status_recovery(app, client, actors, monkeypatch):
    from app import assistant
    from app.db import AsyncDatabase
    app.state.settings.agent_terminal_timeout_seconds = .1
    async def controlled(request, operation, *args):
        if operation is assistant.finish_chat:
            await asyncio.sleep(10)
        return await AsyncDatabase.run(request.app.state.async_db, operation, *args)
    monkeypatch.setattr('app.db.run_sql', controlled)
    async def exercise():
        started = time.monotonic()
        identifier = uuid.uuid4().hex
        result = await chat(ChatInput(message='任务进度', intent='jobs', request_id=identifier), request_for(app, actors['owner']))
        assert time.monotonic() - started < 1
        assert result['status'] == 'failed' and result['failure_reason'] == 'terminal_timeout'
        with app.state.db.Session() as db:
            row = db.scalar(select(AgentRun).where(AgentRun.request_id == identifier))
            assert row.status == 'failed' and row.outputs['failure_reason'] == 'terminal_timeout'
            assert db.get(AgentConversation, row.conversation_id).active_run_id is None
    client.portal.call(exercise)


def test_internal_http_logging_does_not_emit_signed_request_url(app, caplog):
    import logging
    from types import SimpleNamespace
    async def exercise():
        storage = SimpleNamespace(bucket='private', client=SimpleNamespace(generate_presigned_url=lambda *a, **kw:
            'https://storage.test/object?signature=private'))
        reader = AsyncObjectReader(storage, app.state.settings.model_copy(update={'storage_backend': 's3'}))
        sends = []
        def respond(request):
            sends.append(str(request.url))
            return httpx.Response(206, headers={'Content-Length': '3', 'Content-Range': 'bytes 0-2/3'},
                                  stream=httpx.ByteStream(b'abc'))
        reader.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            stream = await reader.open('object', 0, 2)
            assert b''.join([chunk async for chunk in stream.chunks()]) == b'abc'
            await stream.close()
            assert sends == ['https://storage.test/object?signature=private']
        finally:
            await reader.close()
    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())
    assert 'signature=private' not in caplog.text
