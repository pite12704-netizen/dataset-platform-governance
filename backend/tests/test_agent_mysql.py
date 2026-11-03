"""用真实 MySQL 验证 Agent 步骤持久化与模型等待期间撤销会话。"""
from test_review_mysql import mysql_case  # noqa: F401
from test_agent_runtime import test_agent_runtime_uses_real_business_tools_and_persists_steps as persistence_case
from test_agent_runtime import test_session_revoked_during_model_wait_cannot_return_content as revocation_case


def test_mysql_agent_persists_steps_without_holding_model_transaction(mysql_case, monkeypatch):
    app, client, actors = mysql_case
    persistence_case(app, client, actors, monkeypatch)


def test_mysql_agent_revocation_during_model_wait_hides_result(mysql_case, monkeypatch):
    app, client, actors = mysql_case
    revocation_case(app, client, actors, monkeypatch)


def test_mysql_native_async_agent_wait_keeps_pools_free_and_conflicts_same_conversation(mysql_case):
    import asyncio
    import uuid
    import anyio
    import httpx
    from app.main import create_app
    from test_agent_runtime import tool, final
    original, client, actors = mysql_case
    owner = actors['owner']
    from test_lifecycle import create_dataset
    _, version = create_dataset(client, owner)
    cfg = original.state.settings.model_copy(update={'agent_tools_enabled': True,
        'model_url': 'http://127.0.0.1:1234/v1', 'model_name': 'native-async-test',
        'model_resource_group': uuid.uuid4().hex})
    async def exercise():
        app = create_app(cfg)
        arrived, release = asyncio.Event(), asyncio.Event()
        replies = iter([tool(args={'version_id': version['id']}), final([version['id']])])
        async def respond(request):
            arrived.set()
            await release.wait()
            return httpx.Response(200, json=next(replies))
        async with app.router.lifespan_context(app):
            await app.state.model_gateway.client.aclose()
            app.state.model_gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
                initial = await c.post('/api/v1/agent/chat', headers=owner['headers'], json={'message': '任务进度', 'intent': 'jobs'})
                cid = initial.json()['conversation_id']
                payload = {'message': '查找', 'intent': 'agent', 'conversation_id': cid, 'request_id': uuid.uuid4().hex}
                task = asyncio.create_task(c.post('/api/v1/agent/chat', headers=owner['headers'], json=payload))
                try:
                    await asyncio.wait_for(arrived.wait(), 3)
                    assert app.state.async_db.engine.pool.checkedout() == 0
                    assert app.state.db.engine.pool.checkedout() == 0
                    assert anyio.to_thread.current_default_thread_limiter().borrowed_tokens == 0
                    duplicate = await c.post('/api/v1/agent/chat', headers=owner['headers'], json=payload)
                    second = await c.post('/api/v1/agent/chat', headers=owner['headers'], json={**payload, 'request_id': uuid.uuid4().hex})
                    assert duplicate.status_code == second.status_code == 409
                    assert (await c.get('/health/live')).status_code == 200
                    release.set()
                    result = await task
                    assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
                    replay = await c.post('/api/v1/agent/chat', headers=owner['headers'], json=payload)
                    assert replay.json()['replayed'] and replay.json()['run_id'] == result.json()['run_id']
                finally:
                    release.set()
                    if not task.done():
                        await task
    asyncio.run(exercise())


def test_mysql_async_query_pool_backpressure_is_safe_503_and_liveness_remains_responsive(mysql_case):
    import asyncio
    import time
    import httpx
    from sqlalchemy import text
    from app.main import create_app
    original, _, _ = mysql_case
    cfg = original.state.settings.model_copy(update={'database_async_pool_size': 1,
        'database_async_max_overflow': 0, 'database_pool_timeout': .1})
    async def exercise():
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            async with app.state.async_db.Session() as session:
                await session.execute(text('SELECT 1'))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
                    started = time.monotonic()
                    response = await c.get('/api/v1/datasets')
                    assert response.status_code == 503 and response.json()['error'] == 'database_busy'
                    assert response.headers['Retry-After'] == '1'
                    assert time.monotonic() - started < 1
                    assert (await c.get('/health/live')).status_code == 200
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as c:
                assert (await c.get('/api/v1/datasets')).status_code == 200
            task = asyncio.create_task(app.state.async_db.run(lambda db: db.scalar(text('SELECT SLEEP(0.2)'))))
            ticks = 0
            while not task.done():
                await asyncio.sleep(.01)
                ticks += 1
            assert await task == 0 and ticks >= 5
            assert app.state.async_db.engine.pool.checkedout() == 0
    asyncio.run(exercise())
