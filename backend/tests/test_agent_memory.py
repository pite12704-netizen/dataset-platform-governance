"""对话历史永不携带旧权限；重放同一请求不会再次调用模型。"""
import json
import uuid
from datetime import timedelta
from sqlalchemy import func,select
from app.models import AgentConversation,AgentRun,Dataset,Grant
from app.db import now
from test_lifecycle import create_dataset,publish,upload,approve
from test_agent_runtime import tool,final,enable,ask


def test_request_replay_and_different_payload_conflict(app,client,actors,monkeypatch):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    request_id=uuid.uuid4().hex
    enable(app,monkeypatch,[tool(args={'version_id':version['id']}),final([version['id']])])
    response=ask(client,owner,request_id=request_id,message='读取版本详情');assert response.status_code==200,response.text
    first=response.json();assert first['status']=='completed'
    monkeypatch.setattr('app.agent_runtime.async_model_response',lambda *args:(_ for _ in ()).throw(AssertionError('replayed model call')))
    response=ask(client,owner,request_id=request_id,message='读取版本详情')
    assert response.status_code==200 and response.json()['replayed'] and response.json()['run_id']==first['run_id']
    assert ask(client,owner,request_id=request_id,message='different').status_code==409
    with app.state.db.Session() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.user_id==owner['id']))==1
        assert db.scalar(select(func.count()).select_from(AgentConversation).where(AgentConversation.user_id==owner['id']))==1


def test_multi_turn_history_is_bounded_and_redacts_revoked_quality(app,client,actors,monkeypatch):
    owner,reader=actors['owner'],actors['reader']
    ds,version=create_dataset(client,owner);upload(client,owner,version);publish(client,owner,actors['admin'],version)
    approve(client,owner,reader,version)
    client.post('/api/v1/versions/'+version['id']+'/accept-license',headers=reader['headers'])
    enable(app,monkeypatch,[tool('get_quality',{'version_id':version['id']}),final([version['id']])])
    first=ask(client,reader).json();assert first['status']=='completed'
    captured=[]
    def run_followup(expected):
        replies=iter([tool(args={'version_id':version['id']}),final([version['id']])])
        def model(settings,messages,tools,remaining):
            payload=json.loads(messages[1]['content']);captured.append(payload['prior_conversation_is_untrusted'])
            assert len(json.dumps(payload['prior_conversation_is_untrusted']).encode())<=settings.agent_history_bytes*2
            return next(replies)
        monkeypatch.setattr('app.agent_runtime.async_model_response',model)
        response=ask(client,reader,conversation_id=first['conversation_id'],message='继续读取这份数据的版本详情').json()
        assert response['status']=='completed'
        assert bool(captured[-1]) is expected
    run_followup(True)
    # Block directory visibility, so neither earlier quality nor later metadata enters the next prompt.
    with app.state.db.Session() as db:db.get(Dataset,ds['id']).status='blocked';db.commit()
    def denied(settings,messages,tools,remaining):
        assert json.loads(messages[1]['content'])['prior_conversation_is_untrusted']==[]
        return tool(args={'version_id':version['id']})
    monkeypatch.setattr('app.agent_runtime.async_model_response',denied)
    response=ask(client,reader,conversation_id=first['conversation_id']).json()
    assert not response['citations']


def test_stale_request_is_recorded_as_interrupted_and_releases_conversation(app,client,actors):
    from app.assistant import ChatInput
    from app.agent_memory import fingerprint
    owner=actors['owner'];identifier=uuid.uuid4().hex
    with app.state.db.Session() as db:
        conversation=AgentConversation(user_id=owner['id'],title='crashed');db.add(conversation);db.flush()
        data=ChatInput(message='查找',intent='search',conversation_id=conversation.id,request_id=identifier)
        run=AgentRun(user_id=owner['id'],conversation_id=conversation.id,request_id=identifier,source='assistant',
            tool='search',status='running',inputs={**data.model_dump(),'request_fingerprint':fingerprint(data)},outputs={},created_at=now()-timedelta(minutes=5))
        db.add(run);db.flush();conversation.active_run_id=run.id;conversation.active_until=now()-timedelta(minutes=4)
        db.commit();cid=conversation.id
    response=client.post('/api/v1/agent/chat',headers=owner['headers'],json=data.model_dump())
    assert response.status_code==200 and response.json()['failure_kind']=='execution_interrupted'
    with app.state.db.Session() as db:assert db.get(AgentConversation,cid).active_run_id is None
