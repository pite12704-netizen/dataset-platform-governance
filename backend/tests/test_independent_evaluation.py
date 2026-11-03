import json
import uuid
from sqlalchemy import select
from app.models import Asset,Dataset,GovernanceReport,Version
from test_image_reviews import submitted_review,finish_images
from test_lifecycle import create_dataset,upload


def make_auditor(app,client,owner,space,name):
    assert client.post('/api/v1/auth/register',json={'username':name,'email':name+'@example.test','name':name,'password':'Independent-test-password!'}).status_code==201
    result=client.post('/api/v1/auth/login',json={'account':name,'password':'Independent-test-password!'}).json();client.cookies.clear()
    person={'id':result['user']['id'],'headers':{'Authorization':'Bearer '+result['access_token']}}
    person['space_id']=client.get('/api/v1/spaces',headers=person['headers']).json()[0]['id']
    assert client.post('/api/v1/spaces/'+space+'/members',headers=owner['headers'],json={'email':name,'role':'evaluator'}).status_code==200
    return person


def test_blind_truth_never_enters_producer_space_and_requires_designated_approver(app,client,actors):
    owner,first,second,batch,v=submitted_review(app,client,actors,3);finish_images(client,first,second,batch)
    with app.state.db.Session() as db:space=db.get(Dataset,v['dataset_id']).space_id
    maker=make_auditor(app,client,owner,space,'custodian-'+uuid.uuid4().hex[:8]);approver=make_auditor(app,client,owner,space,'assessor-'+uuid.uuid4().hex[:8])
    request={'version_id':v['id'],'reviewer_id':approver['id'],'sample_count':3}
    assert client.post('/api/v1/evaluation-sets',headers=owner['headers'],json=request).status_code==403
    assert client.get('/api/v1/image-reviews/'+batch['id'],headers=maker['headers']).status_code==403
    created=client.post('/api/v1/evaluation-sets',headers=maker['headers'],json=request)
    assert created.status_code==201,created.text
    item=created.json();root='/api/v1/evaluation-sets/'+item['id']
    summary=client.get(root,headers=owner['headers']).json()
    assert 'sample_ids' not in summary and 'truth_asset_id' not in summary
    asset_id=item['sample_ids'][0]
    assert client.get(root+'/assets/'+asset_id+'/preview',headers=owner['headers']).status_code==403
    assert client.get(root+'/assets/'+asset_id+'/preview',headers=maker['headers']).status_code==200
    # Synthetic truth validates the workflow; it is not evidence of real independent quality.
    detail=client.get('/api/v1/image-reviews/'+batch['id'],headers=owner['headers']).json()
    truth={'schema_version':1,'images':[{'asset_id':i['asset_id'],'persons':[i['annotation']]} for i in detail['items']]}
    _,private=create_dataset(client,maker,visibility='private')
    truth_id=upload(client,maker,private,json.dumps(truth).encode(),'private-truth.json')[1]['asset_id']
    assert client.post(root+'/truth',headers=maker['headers'],json={'truth_asset_id':truth_id}).status_code==200
    with app.state.db.Session() as db:assert db.get(Asset,truth_id).version_id!=v['id']
    assert client.post(root+'/run',headers=owner['headers']).status_code==403
    cfg=app.state.settings;cfg.quality_min_truth_images=2;cfg.quality_min_positives=1;cfg.quality_min_negatives=0
    run=client.post(root+'/run',headers=maker['headers'])
    assert run.status_code==201 and run.json()['status']=='completed',run.text
    report_id=run.json()['result']['report_id'];report='/api/v1/governance/reports/'+report_id
    assert client.get(report+'/truth',headers=owner['headers']).status_code==403
    assert client.get(report+'/truth',headers=approver['headers']).status_code==200
    assert client.post(report+'/decision',headers=first['headers'],json={'approve':False}).status_code==403
    result=client.post(report+'/decision',headers=approver['headers'],json={'approve':True,'truth_verified':True})
    assert result.status_code==200 and result.json()['summary']['quality_gate']['passed'],result.text
    producer_reports=client.get('/api/v1/governance/versions/'+v['id'],headers=owner['headers']).json()
    exported=next(r for r in producer_reports if r['id']==report_id)['summary']
    assert 'truth_asset_id' not in exported and 'per_image' not in exported and 'sample_asset_ids' not in exported['sampling']
    # Revision changes invalidate both repeat blind runs and old approval evidence.
    page=detail['images'][0]
    assert client.post('/api/v1/image-reviews/images/'+page['id']+'/reopen',headers=first['headers'],json={'revision':page['revision']}).status_code==200
    assert client.post(root+'/run',headers=maker['headers']).status_code==409


def test_quality_policy_cannot_be_satisfied_by_nonblind_metrics(app):
    from app.governance import quality_gate
    cfg=app.state.settings
    metric={'tp':20,'fn':0,'truth_negative':20,'precision':1,'recall':1,'coverage':1}
    summary={'evaluation_target':'approved_annotations','unique_images':100,'attributes':{'hat':metric,'short_sleeves':metric},
             'detection':{'precision':1,'recall':1}}
    policy={key.removeprefix('quality_'):getattr(cfg,key) for key in cfg.model_fields if key.startswith('quality_min_')}
    policy['require_blind_evaluation']=True
    assert not quality_gate(summary,cfg,policy)['passed']
    assert quality_gate({**summary,'evaluation_set_id':'synthetic-contract-only'},cfg,policy)['passed']
