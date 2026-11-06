"""隔离行为测试：合成文档/图片和可控编码器，不证明真实模型语义准确率。"""
import hashlib
import io
import math
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.db import now
from app.models import Asset, Dataset, KnowledgeDocument, Membership, RetrievalEntry, RetrievalEvent, Version
from app.retrieval_events import enqueue
from app.retrieval_indexing import process_batch, process_one, split_document
from app.retrieval_providers import RetrievalUnavailable, normalize
from test_lifecycle import create_dataset, upload


class Encoder:
    fail = False

    def encode(self, kind, inputs):
        if self.fail:
            raise RetrievalUnavailable("synthetic_unavailable")
        vectors = []
        for value in inputs:
            raw = value if isinstance(value, bytes) else value.encode()
            code = hashlib.sha256(raw).digest()
            vectors.append(normalize([n-127 for n in code], 32))
        return vectors

    def rerank(self, question, passages):
        return [float(len(passages)-i) for i in range(len(passages))]


class Index:
    def __init__(self):
        self.points = {}
        self.last_scope = None
        self.on_search = None

    def upsert(self, kind, points):
        for point in points:
            self.points[kind, point['id']] = point

    def delete(self, kind, identifiers):
        for identifier in identifiers:
            self.points.pop((kind, identifier), None)

    def search(self, kind, vector, scope, limit=60):
        self.last_scope = scope
        if self.on_search:
            self.on_search()
        condition = scope['must'][0]
        rows = [p for (k, _), p in self.points.items() if k == kind and p['payload'].get(condition['key']) in condition['match']['any']]
        return [p['id'] for p in sorted(rows, key=lambda p: -sum(a*b for a, b in zip(vector, p['vector'])))[:limit]]

    def collection(self, kind):
        return 'synthetic_'+kind

    def scroll(self, kind, offset=None):
        return [p for (k, _), p in self.points.items() if k == kind][:128], None


@pytest.fixture
def retrieval(app):
    cfg = app.state.settings
    cfg.retrieval_enabled = True
    cfg.retrieval_text_dimensions = cfg.retrieval_image_dimensions = 32
    app.state.db.Session.configure(info={'retrieval_enabled': True})
    app.state.retrieval_encoder, app.state.retrieval_index = Encoder(), Index()
    return app


def drain(app, max_rounds=12):
    for _ in range(max_rounds):
        process_batch(app.state.db, app.state.storage, app.state.settings,
                      app.state.retrieval_encoder, app.state.retrieval_index)
        with app.state.db.Session() as db:
            if not db.scalar(select(RetrievalEvent.id).where(RetrievalEvent.status.in_(['queued', 'running'])).limit(1)):
                return
    raise AssertionError('synthetic index queue did not finish')


def doc(client, actor, text='独立复核由另一账号完成，发布审核由管理员完成。', **extra):
    response = client.post('/api/v1/retrieval/documents', headers=actor['headers'], json={
        'title': '审核规范', 'body': '# 审核流程\n\n'+text, 'space_id': actor['space_id'], 'status': 'published', **extra})
    assert response.status_code == 201
    return response.json()


def image_version(app, client, actor, count=2):
    app.state.settings.part_size_bytes = 1024*1024
    _, version = create_dataset(client, actor, modality='image')
    for i in range(count):
        out = io.BytesIO()
        Image.new('RGB', (12, 12), (i*40, 80, 100)).save(out, 'PNG')
        upload(client, actor, version, data=out.getvalue(), name=f'picture-{i}.png')
    with app.state.db.Session() as db:
        ids = list(db.scalars(select(Asset.id).where(Asset.version_id == version['id']).order_by(Asset.id)))
    return version, ids


def test_knowledge_permissions_sources_revision_and_no_answer(retrieval, client, actors):
    owner, stranger = actors['owner'], actors['stranger']
    document = doc(client, owner)
    drain(retrieval)
    response = client.post('/api/v1/retrieval/knowledge/ask', headers=owner['headers'], json={'query': '独立复核'})
    assert response.status_code == 200
    assert '另一账号' in response.json()['answer']
    assert response.json()['knowledge_citations'][0]['revision'] == 1
    hidden = client.post('/api/v1/retrieval/knowledge/ask', headers=stranger['headers'], json={'query': '独立复核'})
    assert hidden.status_code == 200 and hidden.json()['knowledge_citations'] == []
    assert client.get('/api/v1/retrieval/documents/'+document['id'], headers=stranger['headers']).status_code == 404
    edited = {k: document[k] for k in ('title', 'body', 'space_id', 'status')}
    edited.update(body='版本2规则已经变更。', expected_revision=1)
    assert client.put('/api/v1/retrieval/documents/'+document['id'], headers=owner['headers'], json=edited).status_code == 200
    assert client.put('/api/v1/retrieval/documents/'+document['id'], headers=owner['headers'], json=edited).status_code == 409
    stale = client.post('/api/v1/retrieval/knowledge/ask', headers=owner['headers'], json={'query': '独立复核'})
    assert stale.status_code == 200 and stale.json()['knowledge_citations'] == []


def test_outbox_rolls_back_and_new_generation_supersedes_old(retrieval, client, actors):
    document = doc(client, actors['owner'])
    with retrieval.state.db.Session() as db:
        before = db.scalar(select(RetrievalEvent.generation).where(RetrievalEvent.source_id == document['id']))
        row = db.get(KnowledgeDocument, document['id'])
        row.body = 'rollback-only'; db.flush(); db.rollback()
    with retrieval.state.db.Session() as db:
        assert db.scalar(select(RetrievalEvent.generation).where(RetrievalEvent.source_id == document['id'])) == before
    original = retrieval.state.retrieval_encoder.encode
    def changed(kind, inputs):
        with retrieval.state.db.Session() as db:
            row = db.get(KnowledgeDocument, document['id']); row.body = '新内容'; row.revision += 1; db.commit()
        return original(kind, inputs)
    retrieval.state.retrieval_encoder.encode = changed
    process_batch(retrieval.state.db, retrieval.state.storage, retrieval.state.settings,
                  retrieval.state.retrieval_encoder, retrieval.state.retrieval_index)
    with retrieval.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(RetrievalEntry)) == 0
        event = db.scalar(select(RetrievalEvent).where(RetrievalEvent.source_id == document['id']))
        assert event.status == 'queued' and event.generation > before


def test_index_retry_lease_recovery_and_compact_rows(retrieval, client, actors):
    document = doc(client, actors['owner'])
    encoder = retrieval.state.retrieval_encoder
    encoder.fail = True
    process_batch(retrieval.state.db, retrieval.state.storage, retrieval.state.settings, encoder, retrieval.state.retrieval_index)
    with retrieval.state.db.Session() as db:
        row = db.scalar(select(RetrievalEvent).where(RetrievalEvent.source_id == document['id']))
        assert row.status == 'queued' and row.error_code == 'synthetic_unavailable'
        row.status = 'running'; row.lease_token = 'expired'; row.lease_until = now()-timedelta(seconds=1); db.commit()
    encoder.fail = False
    drain(retrieval)
    with retrieval.state.db.Session() as db:
        row = db.scalar(select(RetrievalEvent).where(RetrievalEvent.source_id == document['id']))
        assert row.status == 'done' and row.attempts == 2 and row.lease_token is None
        assert not any(c.name in {'body', 'parameters', 'input_assets'} for c in RetrievalEvent.__table__.columns)


def test_revoke_during_retrieval_and_vector_failure_keyword_fallback(retrieval, client, actors):
    owner = actors['owner']; doc(client, owner); drain(retrieval)
    retrieval.state.retrieval_encoder.fail = True
    degraded = client.post('/api/v1/retrieval/knowledge/ask', headers=owner['headers'], json={'query': '复核'})
    assert degraded.status_code == 200 and degraded.json()['status'] == 'degraded'
    assert degraded.json()['knowledge_citations']
    retrieval.state.retrieval_encoder.fail = False
    def revoke():
        with retrieval.state.db.Session() as db:
            membership = db.scalar(select(Membership).where(Membership.user_id == owner['id'], Membership.space_id == owner['space_id']))
            membership.active = False; db.commit()
    retrieval.state.retrieval_index.on_search = revoke
    denied = client.post('/api/v1/retrieval/knowledge/ask', headers=owner['headers'], json={'query': '复核'})
    assert denied.status_code == 200 and denied.json()['knowledge_citations'] == []


def test_catalog_hard_filters_and_private_versions(retrieval, client, actors):
    owner = actors['owner']; ds, version = create_dataset(client, owner)
    with retrieval.state.db.Session() as db:
        db.get(Dataset, ds['id']).description = '夜间道路行人遮挡检测'
        v = db.get(Version, version['id']); v.card = {'usage_policy': {'commercial_use': 'allowed'}, '_secret': 'never-index-this'}
        v.status = 'published'; db.commit()
    drain(retrieval)
    data = {'query': '夜间行人', 'commercial': True, 'license_name': 'CC-BY-4.0', 'modality': 'text'}
    found = client.post('/api/v1/retrieval/catalog/search', headers=owner['headers'], json=data)
    assert found.status_code == 200 and found.json()['items'][0]['version_id'] == version['id']
    assert client.post('/api/v1/retrieval/catalog/search', headers=owner['headers'], json={**data, 'license_name': 'unknown'}).json()['items'] == []
    with retrieval.state.db.Session() as db:
        assert all('never-index-this' not in row.body for row in db.scalars(select(RetrievalEntry)))
        v = db.get(Version, version['id']); v.status = 'draft'; db.commit()
    denied = client.post('/api/v1/retrieval/catalog/search', headers=actors['stranger']['headers'], json=data)
    assert denied.status_code == 200 and denied.json()['items'] == []


def test_images_scope_query_upload_preview_and_fail_closed(retrieval, client, actors):
    owner = actors['owner']; version, ids = image_version(retrieval, client, owner)
    drain(retrieval)
    payload = {'version_id': version['id'], 'asset_id': ids[0]}
    found = client.post('/api/v1/retrieval/images/search', headers=owner['headers'], json=payload)
    assert found.status_code == 200 and found.json()['items'][0]['asset_id'] == ids[1]
    preview = client.get(found.json()['items'][0]['preview_url'], headers=owner['headers'])
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/jpeg'
    assert client.get(found.json()['items'][0]['preview_url'], headers=actors['stranger']['headers']).status_code == 404
    assert client.post('/api/v1/retrieval/images/search', headers=actors['stranger']['headers'], json=payload).status_code == 403
    other, other_ids = image_version(retrieval, client, owner, 1)
    assert client.post('/api/v1/retrieval/images/search', headers=owner['headers'], json={**payload, 'asset_id': other_ids[0]}).status_code == 404
    with retrieval.state.db.Session() as db:
        count = db.scalar(select(func.count()).select_from(Asset))
    stream = io.BytesIO(); Image.new('RGB', (8, 8)).save(stream, 'PNG')
    uploaded = client.post('/api/v1/retrieval/images/query', headers=owner['headers'], data={'version_id': version['id']},
                           files={'file': ('query.png', stream.getvalue(), 'image/png')})
    assert uploaded.status_code == 200
    with retrieval.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(Asset)) == count
    retrieval.state.retrieval_encoder.fail = True
    assert client.post('/api/v1/retrieval/images/search', headers=owner['headers'], json=payload).status_code == 503


def test_reindex_uses_background_cursor_and_requires_owner(retrieval, client, actors):
    version, ids = image_version(retrieval, client, actors['owner'])
    path = '/api/v1/retrieval/versions/'+version['id']+'/index'
    assert client.post(path, headers=actors['stranger']['headers']).status_code == 403
    assert client.post(path, headers=actors['owner']['headers']).status_code == 202
    drain(retrieval)
    status = client.get(path, headers=actors['owner']['headers'])
    assert status.status_code == 200 and status.json()['counts']['done'] >= 4
    with retrieval.state.db.Session() as db:
        row = db.scalar(select(RetrievalEvent).where(RetrievalEvent.kind == 'version_images'))
        assert row.processed == len(ids) and row.cursor


def test_assistant_knowledge_replay_hides_changed_document(retrieval, client, actors):
    owner = actors['owner']; document = doc(client, owner); drain(retrieval)
    response = client.post('/api/v1/agent/chat', headers=owner['headers'], json={'message': '独立复核规范', 'intent': 'knowledge'})
    assert response.status_code == 200 and response.json()['knowledge_citations']
    conversation = response.json()['conversation_id']
    with retrieval.state.db.Session() as db:
        db.get(KnowledgeDocument, document['id']).status = 'archived'; db.commit()
    saved = client.get('/api/v1/agent/conversations/'+conversation, headers=owner['headers'])
    assert saved.status_code == 200 and saved.json()['messages'][0]['outputs']['redacted']


@pytest.mark.parametrize('vector', [[0]*32, [math.nan]*32, [1]*31, ['x']*32, [10**400]*32])
def test_provider_rejects_invalid_vectors(vector):
    with pytest.raises(RetrievalUnavailable):
        normalize(vector, 32)


def test_chunking_preserves_headings_and_long_text():
    rows = split_document('# 第一节\n\n'+('a'*2200)+'\n\n# 第二节\n\n审核要求')
    assert rows[-1][0] == '第二节' and rows[-1][1].endswith('审核要求')
    assert max(len(body) for _, body in rows) <= 1000


def test_global_docs_need_admin_and_document_index_status_hides_token(retrieval, client, actors):
    assert client.post('/api/v1/retrieval/documents', headers=actors['owner']['headers'], json={
        'title': 'Global', 'body': 'Rules', 'status': 'published'}).status_code == 403
    document = doc(client, actors['admin'], space_id=None)
    drain(retrieval)
    result = client.get('/api/v1/retrieval/documents/'+document['id']+'/index', headers=actors['admin']['headers'])
    assert result.status_code == 200 and result.json()['event']['status'] == 'done'
    assert 'lease_token' not in result.json()['event']
    assert client.post('/api/v1/retrieval/documents/'+document['id']+'/index', headers=actors['owner']['headers']).status_code == 403


def test_orphan_reconciliation_keeps_live_and_recent_points(retrieval, client, actors):
    from app.retrieval_indexing import reconcile_orphans
    doc(client, actors['owner']); drain(retrieval)
    index = retrieval.state.retrieval_index
    live = list(index.points)
    old, recent = 'e'*32, 'f'*32
    index.upsert('knowledge', [
        {'id': old, 'vector': [], 'payload': {'created_at': (now()-timedelta(hours=1)).isoformat()}},
        {'id': recent, 'vector': [], 'payload': {'created_at': now().isoformat()}},
    ])
    reconcile_orphans(retrieval.state.db, retrieval.state.settings, index)
    assert ('knowledge', old) not in index.points and ('knowledge', recent) in index.points
    assert all(key in index.points for key in live)


def test_rag_generation_checks_citations_and_falls_back(retrieval, client, actors, monkeypatch):
    from app import agent_runtime
    doc(client, actors['owner']); drain(retrieval)
    retrieval.state.settings.model_url = 'http://127.0.0.1:1/v1'
    retrieval.state.settings.model_name = 'synthetic-model'
    async def model(*args, **kwargs):
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"需由另一账号复核。","citation_ids":[1]}'}}]}
    monkeypatch.setattr(agent_runtime, 'async_model_response', model)
    body = {'query': '独立复核'}
    result = client.post('/api/v1/retrieval/knowledge/ask', headers=actors['owner']['headers'], json=body)
    assert result.status_code == 200 and result.json()['mode'] == 'knowledge-rag'
    async def invalid(*args, **kwargs):
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"unsupported","citation_ids":[999]}'}}]}
    monkeypatch.setattr(agent_runtime, 'async_model_response', invalid)
    fallback = client.post('/api/v1/retrieval/knowledge/ask', headers=actors['owner']['headers'], json=body)
    assert fallback.status_code == 200 and fallback.json()['generation_fallback']
    assert 'unsupported' not in fallback.json()['answer'] and fallback.json()['knowledge_citations']


def test_provider_protocol_rejects_wrong_model_and_qdrant_has_scopes(retrieval, monkeypatch):
    from app import retrieval_providers as p
    cfg = retrieval.state.settings
    monkeypatch.setattr(p, 'request_json', lambda *a, **k: {'model': 'wrong', 'data': []})
    with pytest.raises(RetrievalUnavailable, match='embedding_model_mismatch'):
        p.HTTPEncoder(cfg).encode('knowledge', ['hello'])
    requests = []
    def request(url, method, path, payload=None, *args):
        requests.append((path, payload)); return {'result': []}
    monkeypatch.setattr(p, 'request_json', request)
    index = p.QdrantIndex(cfg)
    original = index.collection('knowledge')
    index.search('knowledge', [0]*32, {'must': [{'key': 'source_id', 'match': {'any': ['permitted']}}]})
    assert requests[-1][1]['filter']['must'][0]['match']['any'] == ['permitted']
    assert 'score_threshold' in requests[-1][1]
    cfg.retrieval_text_model = 'new-revision'
    assert original != index.collection('knowledge')


def test_failed_event_retry_is_authorized_and_reindex_does_not_reset_running_cursor(retrieval, client, actors):
    version, _ = image_version(retrieval, client, actors['owner'])
    path = '/api/v1/retrieval/versions/'+version['id']+'/index'
    client.post(path, headers=actors['owner']['headers'])
    with retrieval.state.db.Session() as db:
        event = db.scalar(select(RetrievalEvent).where(RetrievalEvent.kind == 'version_images'))
        event.cursor = '9'*32; event.status = 'running'; event.lease_until = now()+timedelta(minutes=1)
        identifier, generation = event.id, event.generation; db.commit()
    assert client.post(path, headers=actors['owner']['headers']).status_code == 202
    with retrieval.state.db.Session() as db:
        event = db.get(RetrievalEvent, identifier)
        assert event.generation == generation and event.cursor == '9'*32
        event.status = 'failed'; event.error_code = 'synthetic'; db.commit()
    retry = '/api/v1/retrieval/events/'+identifier+'/retry'
    assert client.post(retry, headers=actors['stranger']['headers']).status_code == 403
    assert client.post(retry, headers=actors['owner']['headers']).status_code == 202


def test_migration_replays_in_empty_isolated_database_and_matches_metadata(tmp_path):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.config import Settings
    from app.db import Database, Base
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root/'backend/alembic.ini'))
    cfg.set_main_option('script_location', str(root/'backend/migrations'))
    settings = Settings(_env_file=None, secret_key='isolated-retrieval-migration-key-00000000000000',
        environment='test', database_url='sqlite:///'+str(tmp_path/'migrations.db'), model_url='', vision_model_url='',
        smtp_host='', retrieval_enabled=False)
    cfg.attributes['settings'] = settings
    command.upgrade(cfg, 'head')
    database = Database(settings.database_url, settings)
    try:
        with database.engine.connect() as conn:
            differences = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        # Other historical migrations deliberately have different index/constraint names;
        # assert the new tables have no structural drift.
        assert not [d for d in differences if 'retrieval_' in str(d) or 'knowledge_documents' in str(d)]
    finally:
        database.engine.dispose()


def test_backup_v12_discards_derived_entries_and_restarts_outbox():
    from scripts.platform_data import canonical, upgrade_manifest
    tables = {'retrieval_entries': [{'id': 'old-vector'}], 'knowledge_documents': [],
        'retrieval_events': [{'id': 'event', 'kind': 'image', 'status': 'running', 'cursor': 'stale', 'lease_token': 'stale'}]}
    manifest = {'schema_version': 12, 'tables': tables, 'metadata_sha256': hashlib.sha256(canonical(tables)).hexdigest()}
    result = upgrade_manifest(manifest)
    assert result['tables']['retrieval_entries'] == []
    event = result['tables']['retrieval_events'][0]
    assert event['status'] == 'queued' and event['cursor'] == '' and event['lease_token'] is None


def test_retrieval_metrics_do_not_inflate_with_duplicate_results():
    from scripts.evaluate_retrieval import metrics
    result = metrics(['a', 'a', 'x', 'b'], ['a', 'b'], 3)
    assert result['recall_at_k'] == 1 and result['precision_at_k'] == pytest.approx(2/3)
    assert result['mrr'] == 1 and 0 < result['ndcg_at_k'] < 1
    assert metrics([], [], 5)['no_answer_correct'] is True


def test_encoder_protocol_authentication_input_and_busy_admission(monkeypatch):
    from fastapi.testclient import TestClient
    from services.retrieval_encoder import server
    monkeypatch.setenv('ENCODER_API_KEY', 'synthetic-encoder-auth')
    monkeypatch.delenv('ENCODER_API_KEY_FILE', raising=False)
    class Encoded:
        def tolist(self):
            return [[1.0, 0.0]]
    class Text:
        def encode(self, inputs, **kwargs):
            return Encoded()
    monkeypatch.setattr(server.app.state, 'text', Text(), raising=False)
    client = TestClient(server.app)  # Deliberately no lifespan/model loading: protocol test only.
    try:
        assert client.post('/embeddings', content=b'not-json').status_code == 401
        headers = {'Authorization': 'Bearer synthetic-encoder-auth'}
        oversized = client.post('/embeddings', headers={**headers, 'Content-Length': str(31*1024**2)}, content=b'{}')
        assert oversized.status_code == 413
        assert client.post('/embeddings', headers=headers, json={'model':'wrong','input':['hello']}).status_code == 422
        data = {'model': 'bge-m3-local-v1', 'input': ['hello']}
        assert client.post('/embeddings', headers=headers, json=data).json()['data'][0]['index'] == 0
        server.gate.acquire()
        try:
            assert client.post('/embeddings', headers=headers, json=data).status_code == 503
        finally:
            server.gate.release()
        bad = {'model':'chinese-clip-local-v1','input':['not-base64']}
        assert client.post('/image-embeddings', headers=headers, json=bad).status_code == 422
    finally:
        client.close()
