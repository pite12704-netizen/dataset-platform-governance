"""Remove conversation content while preserving idempotency and execution receipts."""
from datetime import timedelta
from sqlalchemy import select

from .db import now
from .models import AgentAction, AgentConversation, AgentRun


def redact_run(run):
    run.inputs={key:value for key,value in run.inputs.items() if key=='request_fingerprint'}
    run.outputs={'answer':'此记录内容已清除。执行回执及请求去重记录保留。','citations':[],'redacted':True}
    run.status='redacted'


def redact_action(action):
    action.payload,action.summary={}, {'content_redacted':True}
    if action.status=='pending':action.status='cancelled'


def prune(database,settings):
    cutoff=now()-timedelta(days=settings.agent_content_retention_days)
    with database.Session() as db:
        rows=db.scalars(select(AgentRun).where(AgentRun.created_at<cutoff,AgentRun.status.not_in({'running','redacted'}))
            .order_by(AgentRun.id).limit(200).with_for_update(skip_locked=True)).all()
        for row in rows:redact_run(row)
        actions=db.scalars(select(AgentAction).where(AgentAction.created_at<cutoff,
            AgentAction.summary['content_redacted'].as_boolean().is_not(True))
            .order_by(AgentAction.id).limit(200).with_for_update(skip_locked=True)).all()
        for action in actions:redact_action(action)
        db.commit()
    with database.Session() as db:
        conversations=db.scalars(select(AgentConversation).where(AgentConversation.updated_at<cutoff,
            AgentConversation.active_run_id.is_(None)).order_by(AgentConversation.id).limit(200).with_for_update(skip_locked=True)).all()
        for conversation in conversations:conversation.title='已清除历史内容';conversation.updated_at=now()
        db.commit()
    return {'runs':len(rows),'actions':len(actions)}
