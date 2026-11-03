"""Concurrent confirmation and durable replay on an isolated real MySQL."""
from sqlalchemy import select,func
from app.models import Job
from test_review_mysql import mysql_case,concurrent_posts  # noqa: F401
from test_agent_workflows import vision_case,test_confirm_rejects_changed_receipt_without_consuming_plan as stale_case
from test_agent_workflows import test_confirmed_visual_plan_is_idempotent_and_finishes_recoverable_shards as finish_case
from test_agent_workflows import test_cancelled_plan_recovers_from_persistent_state_with_new_database_instance as recover_case
from test_agent_workspace import prepared,confirm

def test_mysql_concurrent_confirmation_creates_one_parent_and_one_set_of_shards(mysql_case):
    app,client,actors=mysql_case;owner,v=vision_case(app,client,actors)
    action=prepared(client,owner,v,'vision',image_limit=2)
    replies=concurrent_posts(app,[('/api/v1/agent/actions/'+action['id']+'/confirm',owner,{'confirmed':True})]*2)
    assert all(r.status_code in {200,409} for r in replies)
    assert any(r.status_code==200 for r in replies)
    result=confirm(client,owner,action);assert result.status_code==200,result.text
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.version_id==v['id'],Job.kind=='vision_plan'))==1
        assert db.scalar(select(func.count()).select_from(Job).where(Job.version_id==v['id'],Job.kind=='vision'))==2

def test_mysql_receipt_staleness_is_transactional(mysql_case):
    app,client,actors=mysql_case
    stale_case(app,client,actors,'model')

def test_mysql_durable_shards_and_snapshot_stream(mysql_case,monkeypatch):
    app,client,actors=mysql_case
    finish_case(app,client,actors,monkeypatch)

def test_mysql_recovery_with_new_database_instance(mysql_case,monkeypatch):
    app,client,actors=mysql_case
    recover_case(app,client,actors,monkeypatch)
