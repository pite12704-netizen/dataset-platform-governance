"""真实接口治理状态机：独立审核、原图保留、分组隔离和属性误报。"""
import hashlib
import json
import uuid
from sqlalchemy import select
from app.models import Asset,GovernanceReport,ImageReviewBatch,Job,Space,VisionCheckpoint
from app.tasks import run_job
from test_image_reviews import setup_images,import_batch,submitted_review,prediction,finish_images
from test_lifecycle import upload


def queue(client,owner,version,kind,**options):
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={
        'version_id':version['id'],'kind':kind,'idempotency_key':uuid.uuid4().hex,**options})
    assert response.status_code==201,response.text
    assert response.json()['status']=='completed',response.json().get('error')
    return response.json()


def test_dedup_independent_decisions_curate_preserves_originals_and_split_no_leak(app,client,actors):
    owner,reviewer,version,images,_=setup_images(app,client,actors,3)
    with app.state.db.Session() as db:
        original=db.get(Asset,images[0]);raw=b''.join(app.state.storage.chunks(original.object_key))
    duplicate=upload(client,owner,version,raw,'copy.png')[1]['asset_id'];images.append(duplicate)
    job=queue(client,owner,version,'dedup',near_distance=0)
    report=client.get('/api/v1/governance/versions/'+version['id'],headers=reviewer['headers']).json()[0]
    assert report['id']==job['result']['report_id']
    path='/api/v1/governance/reports/'+report['id']+'/decision'
    pairs=report['summary']['pairs'];exact=[p['id'] for p in pairs if p['kind']=='exact']
    assert len(exact)==1
    assert client.post(path,headers=owner['headers'],json={'approve':True}).status_code==403
    assert client.post(path,headers=reviewer['headers'],json={'approve':True,'duplicate_pairs':exact}).status_code==422
    assert client.post(path,headers=actors['stranger']['headers'],json={'approve':False}).status_code==403
    decision={'approve':True,'duplicate_pairs':exact,'reviewed_pairs':[p['id'] for p in pairs]}
    assert client.post(path,headers=reviewer['headers'],json=decision).status_code==200
    assert client.post(path,headers=reviewer['headers'],json=decision).status_code==409
    groups={a:'source-'+a for a in images}
    split=queue(client,owner,version,'split',dedup_report_id=report['id'],groups=groups)
    with app.state.db.Session() as db:
        entries=db.get(GovernanceReport,split['result']['report_id']).summary['entries']
        mapping={e['asset_id']:e['split'] for e in entries}
        assert mapping[images[0]]==mapping[duplicate] and len(set(mapping.values()))==3
        before={a.id:a.sha256 for a in db.scalars(select(Asset).where(Asset.version_id==version['id']))}
    curated=queue(client,owner,version,'curate',dedup_report_id=report['id'])
    with app.state.db.Session() as db:
        original={a.id:a.sha256 for a in db.scalars(select(Asset).where(Asset.version_id==version['id']))}
        derived=db.scalars(select(Asset).where(Asset.version_id==curated['result']['version_id'])).all()
        assert original==before and len(derived)==3 and all(a.status=='clean' for a in derived)
        assert len({a.sha256 for a in derived})==3
        assert db.get(Job,curated['id']).reserved_bytes==0
        space=db.get(Space,db.get(Job,curated['id']).space_id)
        assert space.reserved_bytes==0


def test_evaluation_counts_unmatched_positive_predictions_and_blocks_quality_gate(app,client,actors):
    owner,reviewer,version,images,report_asset=setup_images(app,client,actors,2)
    batch=import_batch(client,owner,report_asset)
    truth={'schema_version':1,'images':[
        {'asset_id':images[0],'persons':[batch['items'][0]['annotation']]},
        {'asset_id':images[1],'persons':[]}]}
    # Use asset association rather than relying on database item ordering.
    truth['images'][0]['persons']=[next(i['annotation'] for i in batch['items'] if i['asset_id']==images[0])]
    truth_id=upload(client,owner,version,json.dumps(truth).encode(),'truth.json')[1]['asset_id']
    job=queue(client,owner,version,'evaluate',truth_asset_id=truth_id,batch_id=batch['id'])
    inbox='/api/v1/governance/evaluations?limit=1'
    reviewer_page=client.get(inbox,headers=reviewer['headers'])
    assert reviewer_page.status_code==200 and reviewer_page.json()['items'][0]['id']==job['result']['report_id']
    assert reviewer_page.json()['items'][0]['can_review'] is True
    assert client.get(inbox,headers=owner['headers']).json()['items'][0]['can_review'] is False
    assert client.get(inbox,headers=actors['stranger']['headers']).json()['items']==[]
    with app.state.db.Session() as db:
        report=db.get(GovernanceReport,job['result']['report_id']);summary=report.summary
        assert summary['attributes']['hat']['tp']==1 and summary['attributes']['hat']['fp']==1
        assert summary['attributes']['hat']['precision']==.5
    path='/api/v1/governance/reports/'+report.id
    assert client.get(path+'/truth',headers=reviewer['headers']).status_code==200
    assert client.get(path+'/truth',headers=actors['stranger']['headers']).status_code==403
    response=client.post(path+'/decision',headers=reviewer['headers'],json={'approve':True,'truth_verified':True})
    assert response.status_code==200 and not response.json()['summary']['quality_gate']['passed']
    assert client.get(inbox,headers=reviewer['headers']).json()['items'][0]['can_review'] is False
    assert client.put('/api/v1/governance/versions/'+version['id']+'/quality-policy',headers=owner['headers'],json={'required':True}).status_code==200
    with app.state.db.Session() as db:
        from app.workflows import require_quality_gate
        from app.models import Version
        import pytest
        from fastapi import HTTPException
        with pytest.raises(HTTPException):require_quality_gate(db,db.get(Version,version['id']),db.scalars(select(Asset).where(Asset.version_id==version['id'])).all())


def test_checkpoint_retry_reuses_completed_picture_and_rejects_changed_pipeline(app,client,actors,monkeypatch):
    owner,_,version,_,_=setup_images(app,client,actors,2)
    cfg=app.state.settings;cfg.vision_enabled=True;cfg.model_url='http://127.0.0.1:1234/v1';cfg.model_name='test';cfg.task_mode='manual'
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':version['id'],'kind':'vision','idempotency_key':uuid.uuid4().hex})
    assert response.status_code==201,response.text
    job_id=response.json()['id'];calls=[]
    def fail_second(*args):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('injected model failure')
        return prediction()
    monkeypatch.setattr('app.vision.predict',fail_second)
    run_job(job_id,app.state.db,app.state.storage,cfg)
    with app.state.db.Session() as db:
        assert db.get(Job,job_id).status=='failed'
        assert len(db.scalars(select(VisionCheckpoint).where(VisionCheckpoint.job_id==job_id)).all())==1
        assert db.scalar(select(ImageReviewBatch)) is None
    calls.clear();monkeypatch.setattr('app.vision.predict',lambda *args:(calls.append(1) or prediction()))
    assert client.post(f'/api/v1/jobs/{job_id}/retry',headers=owner['headers']).status_code==200
    run_job(job_id,app.state.db,app.state.storage,cfg)
    assert len(calls)==1
    with app.state.db.Session() as db:assert db.get(Job,job_id).status=='completed'


def test_zero_person_image_can_be_corrected_and_independently_approved(app,client,actors):
    owner,first,second,batch,version=submitted_review(app,client,actors,1)
    page=client.get('/api/v1/image-reviews/'+batch['id'],headers=first['headers']).json()['images'][0]
    path='/api/v1/image-reviews/images/'+page['id']
    claim=client.post(path+'/claim',headers=first['headers']).json()
    assert client.post(path+'/claim',headers=second['headers']).status_code==409
    response=client.put(path,headers=first['headers'],json={'revision':0,'claim_token':claim['claim_token'],'persons':[]})
    assert response.status_code==200,response.text
    assert response.json()['snapshot']['no_person']
    claim=client.post(path+'/claim',headers=second['headers']).json()
    assert client.post(path+'/approve',headers=second['headers'],json={'revision':1,'claim_token':claim['claim_token']}).status_code==200
    result=client.get('/api/v1/image-reviews/'+batch['id'],headers=owner['headers']).json()
    assert result['status']=='completed' and result['counts']['excluded']==1
    assert client.post(path+'/reopen',headers=first['headers'],json={'revision':1}).status_code==409
    assert client.post(path+'/reopen',headers=first['headers'],json={'revision':2}).status_code==200
    assert client.post('/api/v1/versions/'+version['id']+'/submit',headers=owner['headers']).status_code==409


def test_final_annotation_gate_expires_after_reopen(app,client,actors):
    owner,first,second,batch,version=submitted_review(app,client,actors,2)
    item=batch['items'][1]
    from test_image_reviews import claim_token
    annotation={**item['annotation'],'labels':{**item['annotation']['labels'],'hat':'no_hat','sleeve':'long_sleeves'}}
    response=client.put('/api/v1/image-reviews/items/'+item['id'],headers=first['headers'],json={
        'revision':1,'claim_token':claim_token(client,first,item),'annotation':annotation})
    assert response.status_code==200,response.text
    finish_images(client,first,second,batch)
    detail=client.get('/api/v1/image-reviews/'+batch['id'],headers=owner['headers']).json()
    truth={'schema_version':1,'images':[{'asset_id':i['asset_id'],'persons':[i['annotation']]} for i in detail['items']]}
    truth_id=upload(client,owner,version,json.dumps(truth).encode(),'independent-truth.json')[1]['asset_id']
    cfg=app.state.settings;cfg.quality_min_truth_images=2;cfg.quality_min_positives=1;cfg.quality_min_negatives=1
    job=queue(client,owner,version,'evaluate',truth_asset_id=truth_id,batch_id=batch['id'],evaluation_target='approved_annotations')
    path='/api/v1/governance/reports/'+job['result']['report_id']+'/decision'
    response=client.post(path,headers=second['headers'],json={'approve':True,'truth_verified':True})
    assert response.status_code==200,response.text
    assert response.json()['summary']['quality_gate']['passed'],response.json()['summary']
    client.put('/api/v1/governance/versions/'+version['id']+'/quality-policy',headers=owner['headers'],json={'required':True})
    from app.workflows import require_quality_gate
    from app.models import Version
    with app.state.db.Session() as db:
        require_quality_gate(db,db.get(Version,version['id']),db.scalars(select(Asset).where(Asset.version_id==version['id'])).all())
    page=detail['images'][0]
    assert client.post('/api/v1/image-reviews/images/'+page['id']+'/reopen',headers=first['headers'],json={'revision':page['revision']}).status_code==200
    with app.state.db.Session() as db:
        from fastapi import HTTPException
        import pytest
        with pytest.raises(HTTPException):require_quality_gate(db,db.get(Version,version['id']),db.scalars(select(Asset).where(Asset.version_id==version['id'])).all())
