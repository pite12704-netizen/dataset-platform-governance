import io,zipfile,json,uuid
import pytest
from sqlalchemy import select
from app.models import GovernanceReport,DuplicatePairReview,Membership,ImageReviewImage,Asset
from test_review_workbench import setup_queue,next_image

def setup_joint(app,client,actors):
    owner,a,b,v=setup_queue(app,client,actors)
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':v['id'],'kind':'dedup','near_distance':0,'idempotency_key':uuid.uuid4().hex})
    assert response.status_code==201 and response.json()['status']=='completed',response.text
    report=response.json()['result']['report_id']
    with app.state.db.Session() as db:
        r=db.get(GovernanceReport,report);assert len(r.summary['pairs'])==1
        m=db.scalar(select(Membership).where(Membership.user_id==a['id'],Membership.space_id!=a['space_id']));m.role='member';db.commit()
    body={'annotators':[a['id']],'reviewers':[b['id']],'report_id':report}
    assigned=client.put('/api/v1/joint-review/versions/'+v['id'],headers=owner['headers'],json=body)
    assert assigned.status_code==200,assigned.text
    return owner,a,b,v,report

def save(client,actor,image,decisions=None,excluded=False):
    return client.put('/api/v1/image-reviews/images/'+image['id'],headers=actor['headers'],json={
        'revision':image['revision'],'claim_token':image['claim_token'],'persons':[] if excluded else [{'item_id':i['id'],'annotation':i['annotation']} for i in image['items']],
        'duplicate_decisions':decisions or [],'exclude_duplicate':excluded})

def approve(client,actor,image):
    return client.post('/api/v1/image-reviews/images/'+image['id']+'/approve',headers=actor['headers'],json={'revision':image['revision'],'claim_token':image['claim_token']})

def test_separate_roles_joint_dedup_preserves_raw_and_exports_reviewed_representative(app,client,actors):
    owner,a,b,v,report=setup_joint(app,client,actors)
    same=client.put('/api/v1/joint-review/versions/'+v['id'],headers=owner['headers'],json={'annotators':[b['id']],'reviewers':[b['id']],'report_id':report})
    assert same.status_code==422
    assert next_image(client,b,v).json()['image'] is None
    inbox=client.get('/api/v1/review-workbench',headers=a['headers']);assert inbox.status_code==200,inbox.text
    assert inbox.json()['items'][0]['can_annotate'] is True and not inbox.json()['items'][0]['can_approve']
    first=next_image(client,a,v).json()['image']
    refs=client.get('/api/v1/joint-review/images/'+first['id']+'/similar',headers=a['headers']).json()
    assert len(refs['pairs'])==1 and not refs['pairs'][0]['owned']
    ref=client.get('/api/v1/joint-review/images/'+first['id']+'/reference/'+refs['pairs'][0]['reference_asset_id']+'/preview',headers=a['headers'])
    assert ref.status_code==200 and ref.json()['content'].startswith('data:image/jpeg;base64,')
    assert save(client,a,first).status_code==200
    review=next_image(client,b,v).json()['image']
    assert save(client,b,review).status_code==403
    assert approve(client,a,review).status_code==403
    assert approve(client,b,review).status_code==200
    second=next_image(client,a,v,first['id']).json()['image']
    refs=client.get('/api/v1/joint-review/images/'+second['id']+'/similar',headers=a['headers']).json()
    pair=refs['pairs'][0];assert pair['owned']
    assert save(client,a,second).status_code==409
    done=save(client,a,second,[{'pair_id':pair['id'],'duplicate':True}],True);assert done.status_code==200,done.text
    assert not done.json()['snapshot']['no_person']
    review=next_image(client,b,v).json()['image']
    approved=approve(client,b,review);assert approved.status_code==200,approved.text
    with app.state.db.Session() as db:
        r=db.get(GovernanceReport,report);assert r.status=='approved' and r.decision['duplicate_pairs']==[pair['id']]
        p=db.scalar(select(DuplicatePairReview));assert p.annotator_id==a['id'] and p.reviewer_id==b['id']
        source=list(db.scalars(select(Asset).where(Asset.version_id==v['id'])));assert len([x for x in source if x.filename.endswith('.png')])==2
    count=client.get('/api/v1/deliveries/versions/'+v['id']+'/attributes',headers=owner['headers'])
    assert count.status_code==200,count.text
    assert count.json()['total']==1
    update={'license':v['license'],'source':v['source'],'quality_tier':'curated'}
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json=update).status_code==200
    assert client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==409
    job=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':v['id'],'kind':'release','delivery_format':'coco','idempotency_key':uuid.uuid4().hex})
    assert job.status_code==201 and job.json()['status']=='completed',job.text
    data=client.get('/api/v1/jobs/'+job.json()['id']+'/download',headers=owner['headers'])
    with zipfile.ZipFile(io.BytesIO(data.content)) as package:
        annotations=json.loads(package.read('annotations.json'))
        assert len(annotations['images'])==1 and annotations['images'][0]['source_asset_id']==first['asset_id']
        assert len(annotations['annotations'])==1
    submitted=client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers'])
    assert submitted.status_code==200,submitted.text
    publication=submitted.json();path='/api/v1/publications/'+publication['id']+'/decision'
    assert client.post(path,headers=b['headers'],json={'approve':True}).status_code==403
    with app.state.db.Session() as db:
        from app.models import Dataset,Version
        space_id=db.get(Dataset,db.get(Version,v['id']).dataset_id).space_id
    administrator=actors['stranger']
    member=client.post('/api/v1/spaces/'+space_id+'/members',headers=owner['headers'],json={'email':administrator.get('email','stranger@example.test'),'role':'admin'})
    assert member.status_code==200,member.text
    inbox=client.get('/api/v1/publications',headers=administrator['headers']).json()
    assert next(p for p in inbox if p['id']==publication['id'])['can_review']
    assert client.post(path,headers=administrator['headers'],json={'approve':True}).status_code==200
    from scripts.platform_data import snapshot,upgrade_manifest
    import copy
    with app.state.db.Session() as db:
        table=db.get(ImageReviewImage,review['id']);assert table.snapshot['dedup_excluded'] is True
    state=snapshot(app.state.db);assert state['schema_version']==11
    restored=upgrade_manifest(copy.deepcopy(state));assert restored['tables']['duplicate_pair_reviews'][0]['approved_duplicate'] is True
    bad=copy.deepcopy(state);bad['tables']['duplicate_pair_reviews'][0]['right_asset_id']='f'*32
    import hashlib
    from scripts.platform_data import canonical
    bad['metadata_sha256']=hashlib.sha256(canonical(bad['tables'])).hexdigest()
    with pytest.raises(ValueError):upgrade_manifest(bad)

def test_conflicting_transitive_duplicate_verdicts_are_rejected():
    from types import SimpleNamespace
    from app.joint_review import consistent
    report=SimpleNamespace(id='r',summary={'media_manifest':'m','pairs':[{'id':'ab','left':'a','right':'b'},{'id':'bc','left':'b','right':'c'},{'id':'ac','left':'a','right':'c'}]})
    with pytest.raises(Exception,match='重复关系存在矛盾'):consistent(report,{'ab','bc'})

def test_legacy_submission_without_owned_pairs_does_not_require_reannotation(app,client,actors):
    owner,a,b,v,report=setup_joint(app,client,actors)
    image=next_image(client,a,v).json()['image']
    assert save(client,a,image).status_code==200
    with app.state.db.Session() as db:
        page=db.get(ImageReviewImage,image['id'])
        page.snapshot={k:value for k,value in page.snapshot.items() if not k.startswith('dedup_')}
        db.commit()
    current=next_image(client,b,v).json()['image']
    refs=client.get('/api/v1/joint-review/images/'+image['id']+'/similar',headers=b['headers']).json()
    assert not refs['requires_resubmit'] and not any(p['owned'] for p in refs['pairs'])
    assert approve(client,b,current).status_code==200
    with app.state.db.Session() as db:
        page=db.get(ImageReviewImage,image['id'])
        assert page.editor_id==a['id'] and page.snapshot['dedup_report_id']==report

def test_joint_inputs_are_frozen_and_legacy_item_routes_cannot_bypass(app,client,actors):
    owner,a,b,v,_=setup_joint(app,client,actors)
    image=next_image(client,a,v).json()['image'];item=image['items'][0]
    assert client.post('/api/v1/image-reviews/items/'+item['id']+'/claim',headers=a['headers']).status_code==409
    assert client.delete('/api/v1/assets/'+image['asset_id'],headers=owner['headers']).status_code==409
    assert client.post('/api/v1/versions/'+v['id']+'/uploads',headers=owner['headers'],json={'filename':'extra.png','size':1,'sha256':'0'*64,'idempotency_key':'extra-image'}).status_code==409
