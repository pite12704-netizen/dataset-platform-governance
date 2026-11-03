import io
import json
import uuid
import zipfile
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import select,func

from app.db import now
from app.models import AgentConversation,AgentRun,Asset,Job,User
from app.tasks import run_job
from app.preview_cache import ThumbnailCache
from test_image_reviews import setup_images,import_batch,finish_images,submitted_review,prediction
from test_lifecycle import upload,publish,approve,create_dataset
from test_governance import queue


def test_thumbnail_cache_is_bounded_and_single_flight():
    cache=ThumbnailCache(max_bytes=8,ttl=600)
    entered,release=Event(),Event();calls=[]
    def make():calls.append(1);entered.set();assert release.wait(2);return b'abcd'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(cache.get_or_create,'same',make);assert entered.wait(1)
        second=pool.submit(cache.get_or_create,'same',make);release.set()
        assert first.result()==second.result()==b'abcd'
    assert len(calls)==1
    cache.get_or_create('second',lambda:b'12345678')
    assert cache.bytes<=8 and 'same' not in cache.entries


def test_hot_preview_cache_does_not_cache_reviewer_permission(app,client,actors,monkeypatch):
    owner,first,_,batch,_=submitted_review(app,client,actors,1)
    path=f'/api/v1/image-reviews/{batch["id"]}/assets/{batch["images"][0]["asset_id"]}/preview'
    calls=[];original=app.state.storage.chunks
    def chunks(*args,**kwargs):calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(app.state.storage,'chunks',chunks)
    assert client.get(path,headers=first['headers']).status_code==200
    assert client.get(path,headers=first['headers']).status_code==200
    assert len(calls)==1
    with app.state.db.Session() as db:
        from app.models import Dataset,Version
        space=db.get(Dataset,db.get(Version,batch['version_id']).dataset_id).space_id
    assert client.post('/api/v1/spaces/'+space+'/members',headers=owner['headers'],json={
        'email':'admin@example.test','role':'reviewer','active':False}).status_code==200
    assert client.get(path,headers=first['headers']).status_code==403
    assert len(calls)==1


def test_attribute_and_matches_same_person_and_subset_preserves_all_people(app,client,actors):
    owner,first,v,_,source_id=setup_images(app,client,actors,1);second=actors['reader']
    with app.state.db.Session() as db:
        asset=db.get(Asset,source_id);report=json.loads(b''.join(app.state.storage.chunks(asset.object_key)))
    person=report['images'][0]['persons'][0]
    person['attributes']['sleeve']['label']='long_sleeves'
    other=json.loads(json.dumps(person));other['id']='2';other['bbox']=[35,5,20,60]
    other['attributes']['hat']['label']='no_hat';other['attributes']['sleeve']['label']='short_sleeves'
    report['images'][0]['persons'].append(other)
    source=upload(client,owner,v,json.dumps(report).encode(),'two-people.json')[1]['asset_id']
    batch=import_batch(client,owner,source)
    assert client.post('/api/v1/image-reviews/'+batch['id']+'/submit',headers=owner['headers']).status_code==200
    finish_images(client,first,second,batch)
    path='/api/v1/deliveries/versions/'+v['id']+'/attributes'
    assert client.get(path+'?hat=hat&sleeve=short_sleeves',headers=owner['headers']).json()['total']==0
    assert client.get(path+'?hat=hat&sleeve=long_sleeves',headers=owner['headers']).json()['total']==1
    assert client.get(path+'?hat=invalid',headers=owner['headers']).status_code==422
    assert client.get(path,headers=actors['stranger']['headers']).status_code==403
    build=queue(client,owner,v,'release',selection={'hat':'hat','sleeve':'long_sleeves'})
    raw=client.get('/api/v1/jobs/'+build['id']+'/download',headers=owner['headers'])
    assert raw.status_code==200,raw.text
    with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
        payload=json.loads(archive.read('annotations.json'))
        assert len(payload['images'])==1 and len(payload['annotations'])==2
    assert client.get('/api/v1/deliveries/versions/'+v['id'],headers=owner['headers']).json()['available'] is False


def test_truth_is_not_in_training_grant_preview_agent_or_package(app,client,actors):
    owner,first,second,batch,v=submitted_review(app,client,actors,1)
    finish_images(client,first,second,batch)
    truth={'schema_version':1,'images':[{'asset_id':batch['items'][0]['asset_id'],'persons':[batch['items'][0]['annotation']]}]}
    truth_id=upload(client,owner,v,json.dumps(truth).encode(),'independent-answers.json',preview=True)[1]['asset_id']
    queue(client,owner,v,'evaluate',truth_asset_id=truth_id,batch_id=batch['id'])
    publish(client,owner,first,v);approve(client,owner,second,v)
    assert client.post('/api/v1/versions/'+v['id']+'/accept-license',headers=second['headers']).status_code==200
    assert client.get('/api/v1/assets/'+truth_id+'/download',headers=second['headers']).status_code==403
    assert client.get('/api/v1/assets/'+truth_id+'/preview',headers=second['headers']).status_code==403
    assets=client.get('/api/v1/versions/'+v['id']+'/assets',headers=second['headers']).json()['items']
    assert truth_id not in {a['id'] for a in assets}
    from app.assistant_tools import sample_info
    from fastapi import HTTPException
    import pytest
    with app.state.db.Session() as db:
        with pytest.raises(HTTPException):sample_info(db,db.get(User,second['id']),app.state.settings,app.state.storage,truth_id)
    build=queue(client,second,v,'package')
    response=client.get('/api/v1/jobs/'+build['id']+'/download',headers=second['headers'])
    assert response.status_code==200,response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert not any(truth_id in name for name in archive.namelist())


def test_visual_plan_advances_after_restart_and_cancels_children(app,client,actors,monkeypatch):
    owner,_,v,_,_=setup_images(app,client,actors,3)
    cfg=app.state.settings;cfg.vision_enabled=True;cfg.model_url='http://127.0.0.1:1234/v1';cfg.model_name='synthetic'
    cfg.task_mode='manual';cfg.vision_max_images=1;cfg.vision_plan_active_shards=1
    monkeypatch.setattr('app.vision.predict',lambda *args:prediction())
    response=client.post('/api/v1/jobs/vision-batches',headers=owner['headers'],json={
        'version_id':v['id'],'kind':'vision','idempotency_key':uuid.uuid4().hex})
    assert response.status_code==201,response.text
    assert len(response.json()['job_ids'])==1
    parent=response.json()['plan_id']
    for _ in range(5):
        with app.state.db.Session() as db:
            queued=db.scalars(select(Job.id).where(Job.version_id==v['id'],Job.kind=='vision',Job.status=='queued')).all()
        for job_id in queued:run_job(job_id,app.state.db,app.state.storage,cfg)
        run_job(parent,app.state.db,app.state.storage,cfg)
    with app.state.db.Session() as db:
        plan=db.get(Job,parent)
        assert plan.status=='completed',plan.error
        assert plan.result['completed_images']==3
        assert db.scalar(select(func.count()).select_from(Job).where(Job.version_id==v['id'],Job.kind=='vision'))==3
    cfg.task_mode='inline'
    _,_,another,_,_=setup_images(app,client,actors,1)
    cfg.task_mode='manual'
    created=client.post('/api/v1/jobs/vision-batches',headers=owner['headers'],json={
        'version_id':another['id'],'kind':'vision','idempotency_key':uuid.uuid4().hex}).json()
    assert client.post('/api/v1/jobs/'+created['plan_id']+'/cancel',headers=owner['headers']).status_code==200
    with app.state.db.Session() as db:
        assert all(db.get(Job,identifier).status=='cancelled' for identifier in created['job_ids'])


def test_ownership_transfer_does_not_grant_org_admin_automatic_content(app,client,actors):
    owner,other,admin=actors['owner'],actors['reader'],actors['admin']
    space=client.post('/api/v1/spaces',headers=owner['headers'],json={'name':'交接组织'}).json()
    for target,role in [(other,'contributor'),(admin,'admin')]:
        assert client.post('/api/v1/spaces/'+space['id']+'/members',headers=owner['headers'],json={
            'email':('reader' if target is other else 'admin')+'@example.test','role':role}).status_code==200
    dataset,version=create_dataset(client,owner,visibility='organization',space=space['id'])
    upload(client,owner,version)
    payload={'target_user_id':admin['id'],'reason':'模拟负责人正常交接','current_password':'A-long-test-password!'}
    path='/api/v1/datasets/'+dataset['id']+'/transfer-owner'
    assert client.post(path,headers=admin['headers'],json=payload).status_code==403
    legacy=f'/api/v1/spaces/{space["id"]}/datasets/{dataset["id"]}/transfer'
    assert client.post(legacy,headers=admin['headers'],json={'owner_id':admin['id'],'reason':payload['reason'],
        'current_password':payload['current_password']}).status_code==403
    assert client.get('/api/v1/versions/'+version['id'],headers=admin['headers']).status_code==404
    payload['target_user_id']=other['id']
    assert client.post(path,headers=owner['headers'],json=payload).status_code==200
    assert client.get('/api/v1/versions/'+version['id'],headers=other['headers']).json()['can_manage'] is True
    assert client.get('/api/v1/versions/'+version['id'],headers=owner['headers']).status_code==404
    # Disabling the new owner permits recovery only by another MFA administrator;
    # the administrator cannot nominate themselves as the recovery target.
    assert client.put('/api/v1/admin/users/'+other['id'],headers=admin['headers'],json={'active':False}).status_code==200
    payload['target_user_id']=owner['id']
    assert client.post(path,headers=admin['headers'],json=payload).status_code==403
    with app.state.db.Session() as db:
        from app.models import LoginSession
        for session in db.scalars(select(LoginSession).where(LoginSession.user_id==admin['id'])):session.mfa=True
        db.commit()
    assert client.post(path,headers=admin['headers'],json={**payload,'target_user_id':admin['id']}).status_code==403
    assert client.post(path,headers=admin['headers'],json=payload).status_code==200


def test_clear_conversation_preserves_dedup_tombstone_and_removes_content(app,client,actors):
    owner=actors['owner'];request={'message':'合成私有查询内容','intent':'search','request_id':uuid.uuid4().hex}
    response=client.post('/api/v1/agent/chat',headers=owner['headers'],json=request)
    assert response.status_code==200,response.text
    conversation=response.json()['conversation_id']
    path='/api/v1/agent/conversations/'+conversation+'/clear'
    assert client.post(path,headers=actors['stranger']['headers']).status_code==404
    assert client.post(path,headers=owner['headers']).status_code==200
    replay=client.post('/api/v1/agent/chat',headers=owner['headers'],json=request)
    assert replay.status_code==200 and replay.json().get('redacted'),replay.text
    with app.state.db.Session() as db:
        row=db.scalar(select(AgentRun).where(AgentRun.request_id==request['request_id']))
        assert 'message' not in row.inputs and row.inputs.get('request_fingerprint')
        assert row.status=='redacted'
        assert db.scalar(select(func.count()).select_from(AgentConversation).where(AgentConversation.user_id==owner['id']))==1


def test_hamming_candidates_match_exhaustive_oracle():
    import random
    from app.image_index import HammingIndex
    rng=random.Random(41);index=HammingIndex()
    codes=[rng.getrandbits(16) for _ in range(150)]+[0,0,0]
    for identifier,code in enumerate(codes):index.add(code,identifier)
    for query in [0,65535,*codes[:15]]:
        for radius in [0,2,6,16]:
            actual={identifier for identifier,_ in index.search(query,radius)}
            expected={identifier for identifier,code in enumerate(codes) if (code^query).bit_count()<=radius}
            assert actual==expected


def test_workflow_lists_filter_before_paging(client,actors):
    owner,admin,reader=actors['owner'],actors['admin'],actors['reader']
    for _ in range(2):
        _,v=create_dataset(client,owner)
        upload(client,owner,v);publish(client,owner,admin,v);approve(client,owner,reader,v)
    for kind in ('publications','applications','grants'):
        first=client.get('/api/v1/'+kind+'?paged=true&limit=1',headers=owner['headers'])
        assert first.status_code==200,first.text
        assert first.json()['has_more'] and len(first.json()['items'])==1
        second=client.get('/api/v1/'+kind+'?paged=true&limit=1&offset=1',headers=owner['headers'])
        assert first.json()['items'][0]['id']!=second.json()['items'][0]['id']
        hidden=client.get('/api/v1/'+kind+'?paged=true',headers=actors['stranger']['headers'])
        assert hidden.status_code==200 and hidden.json()['items']==[]
        assert client.get('/api/v1/'+kind+'?limit=1000',headers=owner['headers']).status_code==422


def test_retention_clears_content_but_keeps_request_fingerprint(app,client,actors):
    owner=actors['owner'];request={'message':'仅用于自动保留策略的合成内容','intent':'search','request_id':uuid.uuid4().hex}
    result=client.post('/api/v1/agent/chat',headers=owner['headers'],json=request)
    assert result.status_code==200,result.text
    with app.state.db.Session() as db:
        row=db.get(AgentRun,result.json()['run_id'])
        row.created_at=now()-timedelta(days=31);db.commit()
    from app.agent_retention import prune
    assert prune(app.state.db,app.state.settings)['runs']==1
    assert prune(app.state.db,app.state.settings)['runs']==0
    with app.state.db.Session() as db:
        row=db.get(AgentRun,result.json()['run_id'])
        assert row.status=='redacted' and 'message' not in row.inputs and row.inputs['request_fingerprint']
