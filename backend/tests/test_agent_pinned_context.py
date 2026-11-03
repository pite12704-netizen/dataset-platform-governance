from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from app.db import now
from app.models import AgentConversation,AgentRun,Membership,User
from app.agent_memory import history_context
from test_lifecycle import create_dataset


def test_pinned_sources_outlive_recent_window_but_never_outlive_data_permission(app,client,actors):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        conversation=AgentConversation(user_id=owner['id']);db.add(conversation);db.flush();cid=conversation.id
        rows=[]
        for index in range(12):
            row=AgentRun(user_id=owner['id'],conversation_id=cid,source='assistant',tool='search',status='completed',
                created_at=now()-timedelta(minutes=20-index),inputs={'message':'查找此版本 '+str(index)},
                outputs={'answer':'Historical text is not authoritative','citations':[{'version_id':version['id']}],
                         'access_requirements':[{'version_id':version['id'],'scope':'catalog:read','content':False}]})
            db.add(row);db.flush();rows.append(row.id)
        db.commit();old=rows[0]
    path=f'/api/v1/agent/conversations/{cid}/runs/{old}/pin'
    assert client.post(path,headers=actors['stranger']['headers'],json={'pinned':True}).status_code==404
    assert client.post(path,headers=owner['headers'],json={'pinned':True}).status_code==200
    request=SimpleNamespace(state=SimpleNamespace(scopes=None))
    with app.state.db.Session() as db:
        history,_=history_context(db,db.get(User,owner['id']),cid,request,app.state.settings,'not-a-run')
        entry=next(item for item in history if item['source_run_id']==old)
        assert entry['context_kind']=='source-pointer' and entry['answer']=='' and entry['authoritative_state'] is False
        db.scalar(select(Membership).where(Membership.space_id==owner['space_id'],Membership.user_id==owner['id'])).active=False
        db.commit()
    with app.state.db.Session() as db:
        history,_=history_context(db,db.get(User,owner['id']),cid,request,app.state.settings,'not-a-run')
        assert not history
    assert client.post(path,headers=owner['headers'],json={'pinned':False}).status_code==200
