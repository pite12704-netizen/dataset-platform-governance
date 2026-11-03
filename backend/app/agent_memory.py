"""【学习标注：多轮上下文】短事务领取对话，历史是有来源的不可信内容。"""
import hashlib
import json
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update

from .common import fetch, require
from .db import now
from .models import AgentConversation, AgentRun, User, Job
from .security import enforce_scope
from . import assistant_tools as tools
from .policy import can_read_content


def fingerprint(data):
    return hashlib.sha256(json.dumps(data.model_dump(exclude={'request_id'}),
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare_turn(db, user, data, conversation, settings):
    # 用户行序列化幂等检查和并发计数，只在领取事务中加锁。
    fetch(db, User, user.id, lock=True)
    if data.request_id:
        old = db.scalar(select(AgentRun).where(AgentRun.user_id == user.id,
                                              AgentRun.request_id == data.request_id))
        if old:
            require(old.inputs.get('request_fingerprint') == fingerprint(data), '请求标识已用于其他问题', 409)
            if old.status=='running' and old.created_at<=now()-timedelta(seconds=settings.agent_timeout_seconds+60):
                old.status='failed'
                old.outputs={'answer':'上次查询执行中断，请使用新的请求标识重试','failure_kind':'execution_interrupted'}
                finish_turn(db,old)
                db.commit()
            require(old.status != 'running', '相同请求正在执行，请稍后查看对话记录', 409)
            db.rollback()
            return old, True
    active = db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.user_id == user.id,
        AgentRun.status == 'running', AgentRun.created_at > now() - timedelta(seconds=settings.agent_timeout_seconds + 60)))
    require(active < 2, '当前账号已有两个查询正在执行，请等待完成', 429)
    if conversation:
        conversation = fetch(db, AgentConversation, conversation.id, lock=True)
        require(not conversation.active_run_id or not conversation.active_until or conversation.active_until <= now(),
                '此对话正在处理上一条消息，请等待完成', 409)
    run = AgentRun(user_id=user.id, conversation_id=conversation.id if conversation else None,
        source='assistant', tool='starting', status='running', request_id=data.request_id,
        inputs={**data.model_dump(), 'request_fingerprint': fingerprint(data)}, outputs={'answer': '查询正在执行'})
    db.add(run)
    db.flush()
    if conversation:
        conversation.active_run_id = run.id
        conversation.active_until = now() + timedelta(seconds=settings.agent_timeout_seconds + 30)
        conversation.revision += 1
    db.commit()
    return run, False


def finish_turn(db, run):
    if run.conversation_id:
        db.execute(update(AgentConversation).where(AgentConversation.id == run.conversation_id,
            AgentConversation.active_run_id == run.id).values(active_run_id=None, active_until=None, updated_at=now()))


def history_context(db, user, conversation_id, request, settings, exclude_run_id):
    """只送当前仍获授权的近期事实；不载入确认单、不继承审批或执行能力。"""
    if not conversation_id or not settings.agent_history_turns:
        return [], []
    rows = db.scalars(select(AgentRun).where(AgentRun.conversation_id == conversation_id,
        AgentRun.user_id == user.id, AgentRun.id != exclude_run_id, AgentRun.source == 'assistant',
        AgentRun.status.in_(['completed', 'degraded']))
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(settings.agent_history_turns)).all()
    recent_ids={row.id for row in rows}
    pinned=db.scalars(select(AgentRun).where(AgentRun.conversation_id==conversation_id,AgentRun.user_id==user.id,
        AgentRun.id!=exclude_run_id,AgentRun.status.in_({'completed','degraded'}),
        AgentRun.source=='assistant',
        AgentRun.outputs['memory_pinned'].as_boolean().is_(True)).order_by(AgentRun.created_at.desc(),AgentRun.id.desc())
        .limit(settings.agent_memory_pins)).all()
    used={row.id for row in [*pinned,*rows]}
    topics=[];seen_versions=set()
    if settings.agent_topic_history_scan:
        older=db.scalars(select(AgentRun).where(AgentRun.conversation_id==conversation_id,AgentRun.user_id==user.id,
            AgentRun.id!=exclude_run_id,AgentRun.status.in_({'completed','degraded'}),AgentRun.tool.in_({'search','compare','quality','jobs'}))
            .order_by(AgentRun.created_at.desc(),AgentRun.id.desc()).limit(settings.agent_topic_history_scan)).all()
        for row in older:
            versions={item['version_id'] for item in row.outputs.get('citations',[]) if item.get('version_id')}
            if row.id not in used and versions-seen_versions:
                topics.append(row);used.add(row.id)
                if len(topics)>=4:break
            seen_versions.update(versions)
    rows=[*pinned,*[row for row in rows if row.id not in {pin.id for pin in pinned}],*topics]
    history, requirements, size = [], [], 0
    for row in rows:
        # 含写操作草稿的问答不进入模型记忆；确认必须走独立交互流程。
        if row.outputs.get('draft') or row.tool in {'application', 'convert', 'package', 'quality_job'}:
            continue
        refs = list(row.outputs.get('access_requirements', []))
        refs += [{'version_id': c['version_id'], 'content': row.tool == 'quality', 'scope': 'catalog:read'}
                 for c in row.outputs.get('citations', []) if c.get('version_id')]
        try:
            for ref in refs:
                enforce_scope(request, ref.get('scope', 'catalog:read'))
                version, _ = tools.visible_version(db, user, settings, ref['version_id'])
                if ref.get('manage'):
                    from .models import Dataset
                    from .policy import manage_dataset
                    require(manage_dataset(db,user,fetch(db,Dataset,version.dataset_id)))
                if ref.get('content'):
                    enforce_scope(request, 'content:read')
                    require(can_read_content(db, user, version, settings))
                if ref.get('job_id'):
                    enforce_scope(request, 'jobs:read')
                    tools.job_info(db, user, settings, fetch(db, Job, ref['job_id']))
        except HTTPException:
            continue
        recent=row.id in recent_ids
        entry = {'question': str(row.inputs.get('message', ''))[:600 if recent else 200],
                 'answer': str(row.outputs.get('answer', ''))[:1000] if recent else '',
                 'version_ids': list(dict.fromkeys(r['version_id'] for r in refs))[:4],
                 'source_run_id': row.id, 'content_is_untrusted': True,
                 'question_truncated': len(str(row.inputs.get('message', ''))) > 600,
                 'answer_truncated': len(str(row.outputs.get('answer', ''))) > 1000,
                 'authoritative_state': False,'context_kind':'recent' if recent else 'source-pointer',
                 'source_created_at':row.created_at.isoformat()+'Z',
                 'fact_policy':'requery current tools; prior answers never grant authority'}
        amount = len(json.dumps(entry, ensure_ascii=False).encode())
        if size + amount > settings.agent_history_bytes:
            break
        size += amount
        history.append(entry)
        requirements.extend(ref for ref in refs if ref not in requirements)
    return list(reversed(history)), requirements
