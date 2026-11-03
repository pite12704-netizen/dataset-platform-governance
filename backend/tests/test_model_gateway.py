"""模型故障契约：重试分类、全局额度、熔断代际和授权变化均用可控故障验证。"""
import asyncio
import json
import threading
import time
import uuid

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app import model_gateway as g
from app.agent_runtime import AgentStop, bounded_payload, model_response
from app.config import Settings


@pytest.fixture
def cfg():
    return Settings(_env_file=None, secret_key='x' * 40, environment='test', task_mode='manual',
                    model_url='http://127.0.0.1:1234/v1', model_name='scripted',
                    model_resource_group=uuid.uuid4().hex, model_queue_seconds=0,
                    model_retry_base_seconds=.01, model_breaker_seconds=.1)


def transport(monkeypatch, fn):
    original = httpx.AsyncClient
    monkeypatch.setattr(g.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(fn), **kw))


@pytest.mark.parametrize('key', ['', 'synthetic-provider-key'])
def test_real_http_serialization_omits_empty_authorization_header(cfg, key):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            seen.append(self.headers.get('Authorization'))
            raw = b'{"ok":true}'
            self.send_response(200); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        cfg.model_url = f'http://127.0.0.1:{server.server_port}/v1'; cfg.model_api_key = key
        assert g.completion(cfg, {}, 3, 4096) == {'ok': True}
        assert seen == ['Bearer '+key if key else None]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize('code,retries', [(400, 0), (401, 0), (403, 0), (404, 0), (429, 1), (502, 1), (503, 1), (504, 1)])
def test_retry_only_transient_provider_errors(cfg, monkeypatch, code, retries):
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(code if len(calls) == 1 else 200, json={'ok': True})
    transport(monkeypatch, respond)
    if retries: assert g.completion(cfg, {}, 1, 4096) == {'ok': True}
    else:
        with pytest.raises(g.ModelFailure, match='model_rejected'): g.completion(cfg, {}, 1, 4096)
    assert len(calls) == retries + 1
    assert g.status(cfg)['inflight'] == 0


@pytest.mark.parametrize('header', ['5', '120', 'Thu, 31 Dec 2099 23:59:59 GMT'])
def test_deadline_includes_provider_backoff(cfg, monkeypatch, header):
    calls = []
    def respond(request):
        calls.append(1); return httpx.Response(429, headers={'Retry-After': header})
    transport(monkeypatch, respond)
    with pytest.raises(g.ModelFailure, match='time_budget'): g.completion(cfg, {}, .05, 4096)
    assert len(calls) == 1 and g.status(cfg)['inflight'] == 0


def test_slow_stream_cannot_extend_total_deadline(cfg, monkeypatch):
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(.03)
                yield b' '
    transport(monkeypatch, lambda request: httpx.Response(200, stream=Slow()))
    started = time.monotonic()
    with pytest.raises(g.ModelFailure, match='time_budget'): g.completion(cfg, {}, .1, 4096)
    assert time.monotonic() - started < .5 and g.status(cfg)['inflight'] == 0


def test_open_breaker_allows_one_recovery_probe_and_ignores_old_success(cfg):
    cfg.model_breaker_failures = 1
    a, b = g.LocalCoordinator(cfg), g.LocalCoordinator(cfg)
    assert a.acquire('old-success', 1000)[0] == 0
    assert b.acquire('failure', 1000)[0] == 0
    b.finish('failure', 0, 'failed')
    a.finish('old-success', 0, 'succeeded')
    assert a.acquire('denied', 1000)[0] == 2
    time.sleep(.11)
    state, epoch = a.acquire('probe', 1000)
    assert state == 0 and b.acquire('second-probe', 1000)[0] == 2
    a.finish('probe', epoch, 'succeeded')
    state, epoch = b.acquire('recovered', 1000)
    assert state == 0
    b.finish('recovered', epoch, 'succeeded')
    assert a.snapshot()['inflight'] == 0


def test_different_models_share_capacity_but_have_separate_breakers(cfg):
    cfg.model_max_inflight = 1
    other = cfg.model_copy(update={'model_name': 'vision', 'model_url': 'http://host.docker.internal:1234/v1'})
    a, b = g.LocalCoordinator(cfg), g.LocalCoordinator(other)
    assert a.acquire('chat', 1000)[0] == 0
    assert b.acquire('vision', 1000)[0] == 1
    a.finish('chat', 0, 'failed')
    state, epoch = b.acquire('vision', 1000)
    assert state == 0
    b.finish('vision', epoch, 'succeeded')
    assert a.snapshot()['consecutive_failures'] == 1 and b.snapshot()['consecutive_failures'] == 0


def test_permission_change_before_retry_prevents_second_model_call(cfg, monkeypatch):
    calls, checks = [], []
    def respond(request):
        calls.append(1); return httpx.Response(503)
    def validate():
        checks.append(1)
        if len(checks) > 1: raise AgentStop('authorization_changed')
    transport(monkeypatch, respond)
    with g.authorization_guard(validate), pytest.raises(AgentStop, match=''):
        model_response(cfg, [], [], 1)
    assert len(calls) == 1 and len(checks) == 2 and g.status(cfg)['inflight'] == 0


def test_redis_failure_does_not_fall_back_to_uncoordinated_model_calls(cfg, monkeypatch):
    class Broken(g.LocalCoordinator):
        def acquire(self, *args): raise RedisConnectionError('do not expose credentials')
    monkeypatch.setattr(g, 'coordinator', lambda c: Broken(c))
    transport(monkeypatch, lambda request: (_ for _ in ()).throw(AssertionError('provider should not be called')))
    with pytest.raises(g.ModelFailure, match='model_coordination_unavailable'):
        g.completion(cfg, {}, 1, 4096)


@pytest.mark.parametrize('raw,reason', [(b'x' * 4097, 'model_response_budget'), (b'not-json', 'invalid_model_response')])
def test_invalid_or_oversized_provider_response_is_not_retried(cfg, monkeypatch, raw, reason):
    calls = []
    def respond(request): calls.append(1); return httpx.Response(200, content=raw)
    transport(monkeypatch, respond)
    with pytest.raises(g.ModelFailure, match=reason): g.completion(cfg, {}, 1, 4096)
    assert len(calls) == 1 and g.status(cfg)['inflight'] == 0


def test_compaction_reports_truncation_without_leaking_hidden_fields():
    result = bounded_payload({'files': list(range(30)), 'description': '中' * 700,
                              'object_key': 'secret-storage-path', 'total': 30})
    assert result['data']['total'] == 30 and len(result['data']['files']) == 12
    assert result['coverage']['truncated']
    assert {'path': '$.files', 'kind': 'items', 'total': 30, 'returned': 12} in result['coverage']['cuts']
    assert '$.object_key' in result['coverage']['omitted_fields']
    assert 'secret-storage-path' not in json.dumps(result)


def test_bounded_agent_falls_back_to_readonly_tools_with_current_permission(app):
    from app.agent_runtime import run_loop
    from test_agent_runtime import V1
    calls = []
    def execute(name, args):
        calls.append(name)
        return {}, [{'version_id': V1, 'name': '真实可见版本'}], [{'version_id': V1, 'content': False}]
    def unavailable(*args): raise AgentStop('model_circuit_open')
    result = run_loop(app.state.settings, '查找数据', [V1], None, execute, lambda refs: None, lambda t: None, unavailable)
    assert calls == ['get_version'] and result['status'] == 'degraded'
    assert result['failure_reason'] == 'model_circuit_open' and result['citations'][0]['version_id'] == V1
    assert result['execution']['steps'][1]['mode'] == 'deterministic-fallback'


def test_capacity_rejects_overload_without_holding_new_slots(cfg, monkeypatch):
    cfg.model_max_inflight = 1
    held, release = threading.Event(), threading.Event()
    def respond(request):
        held.set(); assert release.wait(3); return httpx.Response(200, json={'ok': True})
    transport(monkeypatch, respond)
    errors = []
    def first():
        try: g.completion(cfg, {}, 2, 4096)
        except Exception as exc: errors.append(type(exc).__name__)
    worker = threading.Thread(target=first); worker.start(); assert held.wait(2)
    try:
        with pytest.raises(g.ModelFailure, match='model_busy'): g.completion(cfg, {}, 1, 4096)
    finally: release.set(); worker.join(3)
    assert not errors and g.status(cfg)['inflight'] == 0


def test_sync_worker_guard_stays_in_session_owner_thread(cfg, monkeypatch):
    owner = threading.get_ident()
    checks, sends = [], []
    def validate():
        checks.append(threading.get_ident())
    def respond(request):
        sends.append(1)
        return httpx.Response(503 if len(sends) == 1 else 200, json={'ok': True})
    transport(monkeypatch, respond)
    assert g.completion(cfg, {}, 1, 4096, before_attempt=validate) == {'ok': True}
    assert checks == [owner, owner]
