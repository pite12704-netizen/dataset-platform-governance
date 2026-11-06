"""Real LangGraph + isolated SQL, synthetic images/models; no business services."""
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import Database, now
from app.main import create_app
from app.models import (AgentAction, AgentGraphCheckpoint, AgentGraphWorkflow, AgentGraphWrite,
                        Asset, Job, Membership, User)
from test_agent_workspace import confirm, prepared
from test_agent_workflows import vision_case


def graph_case(app, client, actors, conversation_id=None):
    app.state.settings.agent_workflow_engine = 'langgraph'
    owner, version = vision_case(app, client, actors)
    action = prepared(client, owner, version, 'vision', image_limit=2, **(
        {'conversation_id': conversation_id} if conversation_id else {}))
    return owner, version, action


def workflow(client, actor, action):
    return client.get(f"/api/v1/agent/actions/{action['id']}/workflow", headers=actor['headers'])


def resume(client, actor, action, **data):
    return client.post(f"/api/v1/agent/actions/{action['id']}/resume", headers=actor['headers'], json=data)


def jobs(db, version):
    return db.scalar(select(func.count()).select_from(Job).where(Job.version_id == version['id'],
        Job.kind == 'vision_plan'))


def test_actual_stategraph_pauses_before_any_business_effect(app, client, actors):
    owner, version, action = graph_case(app, client, actors)
    assert action['orchestration']['engine'] == 'langgraph'
    assert action['orchestration']['status'] == 'awaiting_confirmation'
    with app.state.db.Session() as db:
        assert jobs(db, version) == 0
        assert db.scalar(select(func.count()).select_from(AgentGraphCheckpoint)) > 1
    assert resume(client, owner, action).status_code == 200
    with app.state.db.Session() as db:
        assert db.get(AgentAction, action['id']).status == 'pending'
        assert jobs(db, version) == 0
    assert workflow(client, actors['stranger'], action).status_code == 404
    assert resume(client, owner, action, approved=True).status_code == 422


def test_confirm_is_idempotent_and_task_wait_does_not_hold_a_session(app, client, actors, monkeypatch):
    owner, version, action = graph_case(app, client, actors)
    result = confirm(client, owner, action)
    assert result.status_code == 200, result.text
    assert result.json()['orchestration']['status'] == 'awaiting_task'
    plan_id = result.json()['result']['job_id']
    assert confirm(client, owner, action).json()['result']['job_id'] == plan_id
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1
        root = db.get(AgentGraphWorkflow, action['id'])
        assert root.lease_token is None and root.lease_until is None
    from app import vision
    from app.tasks import run_job
    from app.vision_plans import advance
    monkeypatch.setattr(vision, 'call_image', lambda *a, **k: ({'objects': [], 'truncated': False}, {'runtime': 'synthetic'}))
    with app.state.db.Session() as db:
        children = db.get(Job, plan_id).parameters['children']
    for child in children:
        run_job(child, app.state.db, app.state.storage, app.state.settings)
    advance(plan_id, app.state.db, app.state.settings)
    assert workflow(client, owner, action).json()['task']['status'] == 'completed'
    recovered = resume(client, owner, action, mode='refresh')
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['orchestration']['status'] == 'completed'
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1


def test_confirmation_survives_new_app_and_database_instances(app, client, actors):
    owner, version, action = graph_case(app, client, actors)
    fresh = create_app(app.state.settings)
    with TestClient(fresh) as second:
        assert workflow(second, owner, action).json()['orchestration']['status'] == 'awaiting_confirmation'
        response = confirm(second, owner, action)
        assert response.status_code == 200, response.text
        assert confirm(second, owner, action).json()['result'] == response.json()['result']
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1


def test_crash_after_business_commit_recovers_without_duplicate_job(app, client, actors, monkeypatch):
    owner, version, action = graph_case(app, client, actors)
    from app import assistant
    original = assistant.execute_action
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic crash after committed business receipt')
    monkeypatch.setattr(assistant, 'execute_action', crash)
    assert confirm(client, owner, action).status_code == 500
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1
        assert db.get(AgentAction, action['id']).status == 'completed'
        assert db.get(AgentGraphWorkflow, action['id']).status == 'interrupted'
    monkeypatch.setattr(assistant, 'execute_action', original)
    fresh = create_app(app.state.settings)
    with TestClient(fresh) as second:
        recovered = resume(second, owner, action)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()['orchestration']['status'] == 'awaiting_task'
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1


@pytest.mark.parametrize('change', ['asset', 'model', 'membership', 'account', 'expiry'])
def test_resume_and_confirmation_revalidate_authority_and_receipt(app, client, actors, change):
    owner, version, action = graph_case(app, client, actors)
    with app.state.db.Session() as db:
        if change == 'asset':
            db.scalar(select(Asset).where(Asset.version_id == version['id'])).sha256 = 'e'*64
        elif change == 'model':
            app.state.settings.vision_model_revision = 'synthetic-new-model'
        elif change == 'membership':
            db.scalar(select(Membership).where(Membership.user_id == owner['id'],
                Membership.space_id != owner['space_id'])).active = False
        elif change == 'account':
            db.get(User, owner['id']).active = False
        else:
            db.get(AgentAction, action['id']).expires_at = now()-timedelta(seconds=1)
        db.commit()
    assert confirm(client, owner, action).status_code in {401, 403, 404, 409}
    with app.state.db.Session() as db:
        assert jobs(db, version) == 0


def test_cancellation_and_conversation_erasure_remove_checkpoint_content(app, client, actors):
    from test_agent_workspace import chat
    app.state.settings.agent_workflow_engine = 'langgraph'
    owner, version = vision_case(app, client, actors)
    conversation = chat(client, owner, message='检查流程', version_ids=[version['id']])['conversation_id']
    action = prepared(client, owner, version, 'vision', image_limit=1, conversation_id=conversation)
    response = client.post('/api/v1/agent/conversations/'+conversation+'/clear', headers=owner['headers'])
    assert response.status_code == 200, response.text
    assert resume(client, owner, action).status_code == 409
    with app.state.db.Session() as db:
        assert db.get(AgentGraphWorkflow, action['id']).status == 'redacted'
        assert db.scalar(select(func.count()).select_from(AgentGraphCheckpoint)) == 0
        assert db.scalar(select(func.count()).select_from(AgentGraphWrite)) == 0
    another = prepared(client, owner, version, 'vision', image_limit=1)
    assert client.post(f"/api/v1/agent/actions/{another['id']}/cancel", headers=owner['headers']).status_code == 200
    assert resume(client, owner, another).status_code == 409
    with app.state.db.Session() as db:
        assert db.get(AgentGraphWorkflow, another['id']).status == 'cancelled'


def test_shared_lease_fences_old_writer_and_expired_process_can_recover(app, client, actors):
    from app.agent_graph_store import SQLGraphSaver, thread_config
    owner, _, action = graph_case(app, client, actors)
    first = SQLGraphSaver(app.state.db, action['id'])
    first.acquire('1'*32)
    other_db = Database(app.state.settings.database_url, app.state.settings)
    second = SQLGraphSaver(other_db, action['id'])
    try:
        with pytest.raises(HTTPException) as collision:
            second.acquire('2'*32)
        assert collision.value.status_code == 409
        with app.state.db.Session() as db:
            db.get(AgentGraphWorkflow, action['id']).lease_until = now()-timedelta(seconds=1)
            db.commit()
        second.acquire('2'*32)
        with pytest.raises(HTTPException):
            first.prune_history()
        first.release('interrupted')
        with app.state.db.Session() as db:
            assert db.get(AgentGraphWorkflow, action['id']).lease_token == '2'*32
        with pytest.raises(HTTPException):
            first.get_tuple(thread_config('f'*32))
        second.release('awaiting_confirmation')
    finally:
        other_db.engine.dispose()
    assert resume(client, owner, action).status_code == 200


def test_snapshots_are_bounded_and_progress_reads_do_not_write_checkpoints(app, client, actors):
    owner, version, action = graph_case(app, client, actors)
    assert confirm(client, owner, action).status_code == 200
    for _ in range(12):
        response = resume(client, owner, action, mode='refresh')
        assert response.status_code == 200, response.text
    with app.state.db.Session() as db:
        before = db.scalar(select(func.count()).select_from(AgentGraphCheckpoint))
        assert before <= 8
        assert db.scalar(select(func.count()).select_from(AgentGraphWrite)) <= 256
        for row in db.scalars(select(AgentGraphCheckpoint)):
            assert len(json.dumps(row.checkpoint).encode()) < 32768
            assert 'input_assets' not in json.dumps(row.checkpoint)
    for _ in range(6):
        assert workflow(client, owner, action).status_code == 200
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(AgentGraphCheckpoint)) == before
        assert jobs(db, version) == 1


def test_serializer_has_closed_types_and_no_general_object_loader():
    from app.agent_graph_store import bounded, unpack
    from langgraph.types import Interrupt
    payload = {'__proto__': 'data', 'value': (Interrupt({'action_id': 'a'*32}, id='b'*32),)}
    restored = unpack(bounded(payload, 8192))
    assert type(restored['value']) is tuple and restored['value'][0].id == 'b'*32
    with pytest.raises(ValueError):
        bounded(object(), 8192)
    with pytest.raises(HTTPException):
        bounded({'text': 'x'*40000}, 8192)
    with pytest.raises(ValueError):
        unpack({'t': 'python.object', 'v': 'os.system'})


def test_per_user_pending_workflow_budget_is_enforced(app, client, actors):
    owner, version, action = graph_case(app, client, actors)
    app.state.settings.agent_graph_max_pending = 1
    response = client.post('/api/v1/agent/actions', headers=owner['headers'],
        json={'version_id': version['id'], 'kind': 'vision', 'image_limit': 1})
    assert response.status_code == 429
    assert workflow(client, owner, action).status_code == 200


def test_simultaneous_confirmation_from_two_api_instances_creates_one_plan(app, client, actors):
    from test_review_mysql import concurrent_posts
    owner, version, action = graph_case(app, client, actors)
    calls = [(f"/api/v1/agent/actions/{action['id']}/confirm", owner, {'confirmed': True})]*2
    replies = concurrent_posts(app, calls)
    assert all(row.status_code in {200, 409} for row in replies)
    assert any(row.status_code == 200 for row in replies)
    assert confirm(client, owner, action).status_code == 200
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1


def test_two_versions_share_atomic_user_preview_budget(app, client, actors):
    from test_review_mysql import concurrent_posts
    app.state.settings.agent_workflow_engine = 'langgraph'
    owner, first = vision_case(app, client, actors)
    app.state.settings.task_mode = 'inline'  # Complete the second version's upload checks first.
    _, second = vision_case(app, client, actors)
    app.state.settings.agent_graph_max_pending = 1
    calls = [('/api/v1/agent/actions', owner, {'kind': 'vision', 'version_id': version['id'], 'image_limit': 1})
             for version in (first, second)]
    replies = concurrent_posts(app, calls)
    assert sorted(response.status_code for response in replies) == [201, 429]
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(AgentGraphWorkflow).where(
            AgentGraphWorkflow.user_id == owner['id'])) == 1


def test_fixed_workflow_needs_no_model_and_disables_hosted_tracing(app, client, actors, monkeypatch):
    from langsmith import Client
    def forbidden(*args, **kwargs):
        raise AssertionError('Hosted tracing must stay disabled')
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setattr(Client, 'create_run', forbidden)
    monkeypatch.setattr(Client, 'batch_ingest_runs', forbidden)
    owner, version, action = graph_case(app, client, actors)
    assert not app.state.settings.agent_tools_enabled
    assert confirm(client, owner, action).status_code == 200
    with app.state.db.Session() as db:
        assert jobs(db, version) == 1


@pytest.mark.parametrize('kind', ['quality', 'dedup', 'convert', 'package'])
def test_non_visual_job_actions_use_the_same_confirmed_graph(app, client, actors, kind):
    app.state.settings.agent_workflow_engine = 'langgraph'
    owner, version = vision_case(app, client, actors)
    app.state.settings.task_mode = 'inline'
    if kind == 'convert':
        from test_lifecycle import create_dataset, upload
        _, version = create_dataset(client, owner)
        upload(client, owner, version)
    extra = {'target_format': 'json'} if kind == 'convert' else {}
    action = prepared(client, owner, version, kind, **extra)
    assert action['orchestration']['status'] == 'awaiting_confirmation'
    result = confirm(client, owner, action)
    assert result.status_code == 200, result.text
    job_id = result.json()['result']['job_id']
    final = resume(client, owner, action, mode='refresh')
    assert final.status_code == 200, final.text
    assert final.json()['orchestration']['status'] == 'completed'
    assert confirm(client, owner, action).json()['result']['job_id'] == job_id


def test_application_graph_submits_request_without_approving_it(app, client, actors):
    from test_lifecycle import create_dataset, publish, upload
    from app.models import Application, Grant
    owner, reader = actors['owner'], actors['reader']
    _, version = create_dataset(client, owner)
    upload(client, owner, version)
    publish(client, owner, actors['admin'], version)
    app.state.settings.agent_workflow_engine = 'langgraph'
    action = prepared(client, reader, version, 'application', purpose='隔离测试研究用途')
    response = confirm(client, reader, action)
    assert response.status_code == 200, response.text
    assert response.json()['orchestration']['status'] == 'completed'
    with app.state.db.Session() as db:
        application = db.get(Application, response.json()['result']['application_id'])
        assert application.status == 'pending'
        assert db.scalar(select(func.count()).select_from(Grant)) == 0


def test_failed_job_waits_for_explicit_retry_then_finishes_same_workflow(app, client, actors):
    from test_lifecycle import create_dataset, upload
    from app.tasks import run_job
    app.state.settings.agent_workflow_engine = 'langgraph'
    owner = actors['owner']
    _, version = create_dataset(client, owner)
    upload(client, owner, version)
    app.state.settings.task_mode = 'manual'
    action = prepared(client, owner, version, 'quality')
    submitted = confirm(client, owner, action).json()
    identifier = submitted['result']['job_id']
    with app.state.db.Session() as db:
        job = db.get(Job, identifier)
        job.status, job.attempts, job.finished_at = 'failed', 1, now()
        db.commit()
    attention = resume(client, owner, action, mode='refresh')
    assert attention.status_code == 200, attention.text
    assert attention.json()['orchestration']['status'] == 'task_needs_attention'
    with app.state.db.Session() as db:
        assert db.get(Job, identifier).status == 'failed'  # Refresh never silently retries.
    assert client.post('/api/v1/jobs/'+identifier+'/retry', headers=owner['headers']).status_code == 200
    run_job(identifier, app.state.db, app.state.storage, app.state.settings)
    completed = resume(client, owner, action, mode='refresh')
    assert completed.status_code == 200, completed.text
    assert completed.json()['orchestration']['status'] == 'completed'
    assert completed.json()['result']['job_id'] == identifier


def test_generic_job_preview_rejects_files_added_after_confirmation_preview(app, client, actors):
    from test_lifecycle import create_dataset, upload
    app.state.settings.agent_workflow_engine = 'langgraph'
    owner = actors['owner']
    _, version = create_dataset(client, owner)
    upload(client, owner, version)
    action = prepared(client, owner, version, 'quality')
    upload(client, owner, version, b'field,value\na,1\n', 'new.csv')
    response = confirm(client, owner, action)
    assert response.status_code == 409 and '重新预览' in response.text
    with app.state.db.Session() as db:
        assert db.get(AgentAction, action['id']).status == 'pending'
        assert db.scalar(select(func.count()).select_from(Job).where(
            Job.version_id == version['id'], Job.kind == 'quality')) == 0


def test_backup_clears_resumable_approvals_on_restore(app, client, actors):
    import copy
    from scripts.platform_data import snapshot, upgrade_manifest
    owner, _, action = graph_case(app, client, actors)
    source = snapshot(app.state.db)
    assert source['schema_version'] == 13
    restored = upgrade_manifest(copy.deepcopy(source))
    assert restored['tables']['agent_graph_checkpoints'] == []
    assert restored['tables']['agent_graph_writes'] == []
    row = next(row for row in restored['tables']['agent_graph_workflows'] if row['action_id'] == action['id'])
    assert row['status'] == 'invalidated' and row['lease_token'] is None
    receipt = next(row for row in restored['tables']['agent_actions'] if row['id'] == action['id'])
    assert receipt['status'] == 'cancelled'
    assert len(source['tables']['agent_graph_checkpoints']) > 0


def test_d91_backup_and_frozen_new_migration_use_expected_tables(tmp_path):
    from alembic import command
    from alembic.config import Config
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.config import Settings
    from app.db import Base
    from scripts.platform_data import snapshot, upgrade_manifest
    settings = Settings(_env_file=None, environment='test', database_url='sqlite:///'+str(tmp_path/'migration.db'),
        secret_key='isolated-langgraph-migration-only-0123456789')
    config = Config('backend/alembic.ini')
    config.attributes['settings'] = settings
    command.upgrade(config, 'd91e7a62b430')
    database = Database(settings.database_url, settings)
    try:
        old = snapshot(database)
        assert old['schema_version'] == 12 and len(old['tables']) == 45
        assert upgrade_manifest(old)['schema_version'] == 13
        command.upgrade(config, 'head')
        state = snapshot(database)
        assert state['schema_version'] == 13 and len(state['tables']) == 48
        with database.engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert not [x for x in differences if 'agent_graph_' in str(x)]
    finally:
        database.engine.dispose()
