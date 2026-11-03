import json
import pytest
from sqlalchemy import select,func
from app.models import Job,AgentAction,Version,Membership,Asset,User
from app.tasks import run_job
from app.vision_plans import advance
from test_image_reviews import setup_images
from test_agent_workspace import chat,prepared,confirm
from test_lifecycle import create_dataset,upload
from test_generic_review_templates import bind

def vision_case(app,client,actors):
    owner,_,version,_,_=setup_images(app,client,actors,2)
    bind(client,owner,version)
    settings=app.state.settings
    settings.vision_enabled=True;settings.vision_model_url='http://127.0.0.1:1234/v1';settings.vision_model_name='synthetic-vl'
    settings.vision_model_revision='synthetic-r1';settings.task_mode='manual';settings.vision_max_images=1
    return owner,version

def test_confirmed_visual_plan_is_idempotent_and_finishes_recoverable_shards(app,client,actors,monkeypatch):
    owner,v=vision_case(app,client,actors)
    result=chat(client,owner,message='对所选版本进行图像识别，先做2张',version_ids=[v['id']])
    assert result['draft']['kind']=='vision' and result['draft']['image_limit']==2
    action=prepared(client,owner,v,'vision',image_limit=2)
    assert action['summary']['workflow']['images']==2 and action['summary']['workflow']['template_name']=='车辆检查'
    assert len(json.dumps(action['summary']).encode())<8192
    with app.state.db.Session() as db:assert db.scalar(select(func.count()).select_from(Job).where(Job.version_id==v['id'],Job.kind=='vision_plan'))==0
    response=confirm(client,owner,action);assert response.status_code==200,response.text
    plan_id=response.json()['result']['plan_id']
    assert confirm(client,owner,action).json()['result']['plan_id']==plan_id
    assert client.get('/api/v1/agent/actions/'+action['id']+'/status',headers=actors['stranger']['headers']).status_code==404
    from app import vision
    monkeypatch.setattr(vision,'call_image',lambda *a,**k:({'objects':[{'category':'car','bbox':[20,20,500,800],
        'labels':{'color':'red','damaged':False,'count':0,'note':None}}],'truncated':False},{'runtime':'synthetic'}))
    with app.state.db.Session() as db:
        parent=db.get(Job,plan_id);children=parent.parameters['children']
        assert len(children)==2 and parent.parameters['agent_action_id']==action['id']
        child=db.get(Job,children[0]);child.status='running';child.progress=50;db.commit()
    partial=client.get('/api/v1/agent/actions/'+action['id']+'/status',headers=owner['headers']).json()
    assert partial['progress']==25 and partial['committed_progress']==0 and partial['progress_basis']
    with app.state.db.Session() as db:
        child=db.get(Job,children[0]);child.status='queued';child.progress=0;db.commit()
    for identifier in children:run_job(identifier,app.state.db,app.state.storage,app.state.settings)
    advance(plan_id,app.state.db,app.state.settings)
    status=client.get('/api/v1/agent/actions/'+action['id']+'/status',headers=owner['headers'])
    assert status.status_code==200,status.text
    assert status.json()['status']=='completed' and status.json()['counts']['completed_images']==2
    events=client.get('/api/v1/agent/actions/'+action['id']+'/events',headers=owner['headers'])
    assert events.status_code==200 and 'event: snapshot' in events.text
    event_id=next(l.removeprefix('id: ') for l in events.text.splitlines() if l.startswith('id: '))
    assert len(event_id)==64
    again=client.get('/api/v1/agent/actions/'+action['id']+'/events',headers={**owner['headers'],'Last-Event-ID':event_id})
    assert 'event: snapshot' in again.text
    assert app.state.agent_status_limiter.borrowed_tokens==0 and app.state.agent_status_owners=={}

@pytest.mark.parametrize('change',['input','template','model','metadata','shard'])
def test_confirm_rejects_changed_receipt_without_consuming_plan(app,client,actors,change):
    owner,v=vision_case(app,client,actors);action=prepared(client,owner,v,'vision',image_limit=1)
    if change=='input':
        with app.state.db.Session() as db:
            asset=db.scalar(select(Asset).where(Asset.version_id==v['id'],Asset.filename.like('%.png')));asset.sha256='a'*64;db.commit()
    elif change=='template':
        from test_generic_review_templates import TEMPLATE
        assert client.put('/api/v1/review-templates/versions/'+v['id'],headers=owner['headers'],json={**TEMPLATE,'name':'Changed'}).status_code==200
    elif change=='model':app.state.settings.vision_model_revision='synthetic-r2'
    elif change=='shard':app.state.settings.vision_max_images=2
    else:
        with app.state.db.Session() as db:db.get(Version,v['id']).license='changed';db.commit()
    denied=confirm(client,owner,action)
    assert denied.status_code==409 and '重新预览' in denied.text
    with app.state.db.Session() as db:
        assert db.get(AgentAction,action['id']).status=='pending'
        assert db.scalar(select(func.count()).select_from(Job).where(Job.version_id==v['id'],Job.kind=='vision_plan'))==0

def test_dedup_confirmation_keeps_sources_and_does_not_approve_report(app,client,actors):
    owner,_,v,_,_=setup_images(app,client,actors,2)
    action=prepared(client,owner,v,'dedup',near_distance=0)
    with app.state.db.Session() as db:before=db.scalar(select(func.count()).select_from(Asset))
    result=confirm(client,owner,action);assert result.status_code==200,result.text
    job_id=result.json()['result']['job_id']
    assert confirm(client,owner,action).json()['result']['job_id']==job_id
    from app.models import GovernanceReport
    with app.state.db.Session() as db:
        assert db.get(Job,job_id).status=='completed'
        assert db.scalar(select(GovernanceReport).where(GovernanceReport.kind=='dedup')).status=='pending'
        assert db.scalar(select(func.count()).select_from(Asset))==before

def test_diagnosis_is_read_only_and_rechecks_owner_for_saved_answers(app,client,actors):
    owner=actors['owner'];_,v=create_dataset(client,owner);upload(client,owner,v)
    before=client.get('/api/v1/jobs',headers=owner['headers']).json()
    result=chat(client,owner,message='为什么不能发布，下一步做什么？',version_ids=[v['id']])
    assert result['intent']=='diagnose' and result['diagnosis']['facts_source']=='实时数据库聚合'
    assert client.get('/api/v1/jobs',headers=owner['headers']).json()==before
    with app.state.db.Session() as db:
        from app.models import Dataset
        db.get(Dataset,v['dataset_id']).owner_id=actors['stranger']['id'];db.commit()
    history=client.get('/api/v1/agent/conversations/'+result['conversation_id'],headers=owner['headers']).json()
    assert history['messages'][0]['outputs']['redacted']

def test_sse_hides_data_after_membership_revocation_and_releases_capacity(app,client,actors,monkeypatch):
    owner,v=vision_case(app,client,actors);action=prepared(client,owner,v,'vision',image_limit=1)
    assert confirm(client,owner,action).status_code==200
    from app import agent_streams
    original=agent_streams.state_snapshot;calls=[]
    def snapshot(db,request,user_id,action_id):
        calls.append(True)
        if len(calls)==2:
            with app.state.db.Session() as other:
                membership=other.scalar(select(Membership).where(Membership.user_id==owner['id'],Membership.space_id!=owner['space_id']))
                membership.active=False;other.commit()
        return original(db,request,user_id,action_id)
    monkeypatch.setattr(agent_streams,'state_snapshot',snapshot)
    app.state.settings.agent_status_poll_seconds=.01
    events=client.get('/api/v1/agent/actions/'+action['id']+'/events',headers=owner['headers'])
    assert events.status_code==200 and events.text.count('event: snapshot')==1 and 'event: access_lost' in events.text
    assert not app.state.agent_status_owners and app.state.agent_status_limiter.borrowed_tokens==0

def test_workflow_scope_limits_and_no_silent_count_clamping(app,client,actors):
    owner,v=vision_case(app,client,actors)
    result=chat(client,owner,message='图像识别1001张',version_ids=[v['id']])
    assert result['status']=='failed' and 'draft' not in result
    assert client.post('/api/v1/agent/actions',headers=owner['headers'],json={'kind':'vision','version_id':v['id'],'image_limit':1001}).status_code==422
    assert client.post('/api/v1/agent/actions',headers=actors['reader']['headers'],json={'kind':'vision','version_id':v['id'],'image_limit':1}).status_code in {403,404}
    action=prepared(client,owner,v,'vision',image_limit=1);assert confirm(client,owner,action).status_code==200
    app.state.agent_status_owners[owner['id']]=2
    assert client.get('/api/v1/agent/actions/'+action['id']+'/events',headers=owner['headers']).status_code==429
    app.state.agent_status_owners.clear()
    with app.state.db.Session() as db:
        db.get(User,owner['id']).active=False;db.commit()
    assert client.get('/api/v1/agent/actions/'+action['id']+'/status',headers=owner['headers']).status_code==401

def test_model_tool_diagnosis_uses_real_authorized_facts(app,client,actors,monkeypatch):
    from test_agent_runtime import tool,final
    owner=actors['owner'];_,v=create_dataset(client,owner);upload(client,owner,v)
    settings=app.state.settings;settings.agent_tools_enabled=True;settings.model_url='http://127.0.0.1:1234/v1';settings.model_name='scripted-model'
    replies=iter([tool('diagnose_version',{'version_id':v['id']}),final([v['id']])])
    async def model(*args,**kwargs):return next(replies)
    monkeypatch.setattr('app.agent_runtime.async_model_response',model)
    result=chat(client,owner,message='检查流程状态',intent='agent',version_ids=[v['id']])
    assert result['status']=='completed' and result['execution']['tool_calls']==1
    assert result['access_requirements'][0]['manage'] and result['citations'][0]['version_id']==v['id']

def test_status_reads_do_not_update_parent_job_or_cold_manifest(app,client,actors):
    from sqlalchemy import event
    from app.models import JobPayload
    owner,v=vision_case(app,client,actors);action=prepared(client,owner,v,'vision',image_limit=1)
    assert confirm(client,owner,action).status_code==200
    with app.state.db.Session() as db:before=db.scalar(select(func.count()).select_from(JobPayload))
    writes=[]
    def inspect_sql(conn,cursor,statement,parameters,context,many):
        if statement.lstrip().lower().startswith(('update jobs','insert into job_payloads','update job_payloads')):writes.append(True)
    event.listen(app.state.db.engine,'before_cursor_execute',inspect_sql)
    try:
        for _ in range(30):assert client.get('/api/v1/agent/actions/'+action['id']+'/status',headers=owner['headers']).status_code==200
    finally:event.remove(app.state.db.engine,'before_cursor_execute',inspect_sql)
    assert not writes
    with app.state.db.Session() as db:assert db.scalar(select(func.count()).select_from(JobPayload))==before

def test_cancelled_plan_recovers_from_persistent_state_with_new_database_instance(app,client,actors,monkeypatch):
    owner,v=vision_case(app,client,actors);action=prepared(client,owner,v,'vision',image_limit=2)
    result=confirm(client,owner,action);assert result.status_code==200,result.text
    parent=result.json()['result']['plan_id']
    assert client.post('/api/v1/jobs/'+parent+'/cancel',headers=owner['headers']).status_code==200
    with app.state.db.Session() as db:
        children=db.get(Job,parent).parameters['children']
        assert len(children)==2 and all(db.get(Job,j).status=='cancelled' for j in children)
    from app import vision
    monkeypatch.setattr(vision,'call_image',lambda *a,**k:({'objects':[],'truncated':False},{'runtime':'synthetic'}))
    for identifier in children:
        assert client.post('/api/v1/jobs/'+identifier+'/retry',headers=owner['headers']).status_code==200
        run_job(identifier,app.state.db,app.state.storage,app.state.settings)
    assert client.post('/api/v1/jobs/'+parent+'/retry',headers=owner['headers']).status_code==200
    from app.db import Database
    fresh=Database(app.state.settings.database_url,app.state.settings)
    try:advance(parent,fresh,app.state.settings)
    finally:fresh.engine.dispose()
    status=client.get('/api/v1/agent/actions/'+action['id']+'/status',headers=owner['headers']).json()
    assert status['status']=='completed' and status['counts']['completed_images']==2
    assert confirm(client,owner,action).json()['result']['plan_id']==parent
