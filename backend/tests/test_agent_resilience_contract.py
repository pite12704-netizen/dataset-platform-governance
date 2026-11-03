"""Agent 恢复入口的身份隔离、分页契约和任务队列一致性。"""
from types import SimpleNamespace
import uuid

from sqlalchemy import select

from app.agent_tools import SearchPageArgs, invoke_tool
from app.models import AgentRun, Dataset, Job, User
from app.tasks import dispatch, job_queue
from test_lifecycle import create_dataset


def test_saved_request_is_private_and_replay_does_not_create_another_run(app, client, actors):
    owner, reader = actors['owner'], actors['reader']
    ds, _ = create_dataset(client, owner, visibility='private')
    payload = {'message': '寻找训练语料', 'intent': 'search', 'request_id': uuid.uuid4().hex}
    first = client.post('/api/v1/agent/chat', headers=owner['headers'], json=payload).json()
    path = '/api/v1/agent/requests/' + payload['request_id']
    saved = client.get(path, headers=owner['headers'])
    assert saved.status_code == 200 and saved.json()['id'] == first['run_id']
    assert client.get(path, headers=reader['headers']).status_code == 404
    assert client.get(path).status_code == 401
    replay = client.post('/api/v1/agent/chat', headers=owner['headers'], json=payload).json()
    assert replay['run_id'] == first['run_id']
    with app.state.db.Session() as db:
        assert len(db.scalars(select(AgentRun).where(AgentRun.request_id == payload['request_id'])).all()) == 1
        db.get(Dataset, ds['id']).status = 'blocked'; db.commit()
    revoked = client.get(path, headers=owner['headers']).json()
    assert not revoked['outputs'].get('citations')


def test_search_page_never_leaks_hidden_catalog_and_reports_more(app, client, actors):
    for i in range(11): create_dataset(client, actors['owner'], visibility='private')
    with app.state.db.Session() as db:
        owner, stranger = db.get(User, actors['owner']['id']), db.get(User, actors['stranger']['id'])
        first = invoke_tool('search_datasets_page', SearchPageArgs(query='训练语料', limit=8), db, owner, app.state.settings, None)
        assert first['returned'] == 8 and first['has_more'] and first['next_offset'] == 8
        second = invoke_tool('search_datasets_page', SearchPageArgs(query='训练语料', offset=8), db, owner, app.state.settings, None)
        assert second['returned'] == 3 and not second['has_more'] and second['total_is_unknown']
        assert not set(r['id'] for r in first['items']) & set(r['id'] for r in second['items'])
        hidden = invoke_tool('search_datasets_page', SearchPageArgs(query='训练语料'), db, stranger, app.state.settings, None)
        assert hidden['items'] == [] and not hidden['has_more']


def test_dispatch_routes_from_persisted_job_kind_not_client_queue(app, client, actors, monkeypatch):
    _, version = create_dataset(client, actors['owner'], visibility='private')
    with app.state.db.Session() as db:
        jobs = [Job(version_id=version['id'], user_id=actors['owner']['id'], space_id=actors['owner']['space_id'], kind=kind, idempotency_key=uuid.uuid4().hex)
                for kind in ('vision', 'scan')]
        db.add_all(jobs); db.commit(); ids = [j.id for j in jobs]
    calls = []
    monkeypatch.setattr('app.tasks.celery_app', lambda cfg: SimpleNamespace(send_task=lambda *a, **kw: calls.append(kw)))
    app.state.settings.task_mode = 'celery'
    for identifier in ids: dispatch(app, identifier)
    assert [r['queue'] for r in calls] == ['platform.vision', 'celery']
    assert all(r['retry'] is False for r in calls)
    assert job_queue('quality') == 'celery'


def test_recovery_dispatch_uses_same_queue_mapping(app, client, actors, monkeypatch):
    from app import tasks
    _, version = create_dataset(client, actors['owner'], visibility='private')
    with app.state.db.Session() as db:
        for kind in ('vision', 'scan'):
            db.add(Job(version_id=version['id'], user_id=actors['owner']['id'], space_id=actors['owner']['space_id'],
                       kind=kind, idempotency_key=uuid.uuid4().hex))
        db.commit()
    monkeypatch.setattr(tasks, '_celery', None)
    monkeypatch.setattr(tasks, 'get_settings', lambda: app.state.settings)
    monkeypatch.setattr(tasks, 'Database', lambda *a: app.state.db)
    monkeypatch.setattr(tasks, 'make_storage', lambda *a: app.state.storage)
    monkeypatch.setattr(tasks, 'sweep_storage', lambda *a: None)
    monkeypatch.setattr('app.auth.deliver_mail', lambda *a: None)
    celery = tasks.celery_app(app.state.settings)
    calls = []
    monkeypatch.setattr(celery, 'send_task', lambda *a, **kw: calls.append(kw))
    celery.tasks['dataset.sweep'].run()
    assert sorted(c['queue'] for c in calls) == ['celery', 'platform.vision']
    assert all(c['retry'] is False for c in calls)


def test_legacy_metadata_retries_revalidate_identity(app, client, actors, monkeypatch):
    import httpx
    from app import model_gateway
    create_dataset(client, actors['owner'], visibility='private')
    cfg = app.state.settings
    cfg.model_url, cfg.model_name, cfg.model_resource_group = 'http://127.0.0.1:1234/v1', 'mock', uuid.uuid4().hex
    cfg.model_retry_base_seconds = .01
    calls = []
    def respond(request):
        calls.append(1)
        with app.state.db.Session() as db:
            db.get(User, actors['owner']['id']).active = False
            db.commit()
        return httpx.Response(503)
    original = httpx.AsyncClient
    async def install_transport():
        await app.state.model_gateway.client.aclose()
        app.state.model_gateway.client = original(transport=httpx.MockTransport(respond))
    client.portal.call(install_transport)
    response = client.post('/api/v1/agent/chat', headers=actors['owner']['headers'],
                           json={'message': '寻找训练语料', 'intent': 'search', 'request_id': uuid.uuid4().hex})
    assert response.status_code == 200 and response.json()['status'] == 'failed', response.text
    assert response.json()['citations'] == [] and len(calls) == 1


def test_model_status_is_admin_only_and_contains_no_credentials(client, actors):
    path = '/api/v1/admin/model-status'
    assert client.get(path, headers=actors['owner']['headers']).status_code == 403
    result = client.get(path, headers=actors['admin']['headers'])
    assert result.status_code == 200
    assert 'api_key' not in result.text and 'model_url' not in result.text
