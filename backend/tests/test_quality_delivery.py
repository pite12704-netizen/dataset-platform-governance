"""Publication evidence and deliverables use isolated synthetic assets only."""
import hashlib
import io
import json
import zipfile

from sqlalchemy import select

from app.models import Asset, GovernanceReport, Version
from test_image_reviews import setup_images, import_batch, finish_images, submitted_review, claim_token
from test_governance import queue
from test_imports import create_import, register, transfer, png
from test_lifecycle import create_dataset, upload, publish, approve


def test_cancelled_partial_import_requires_explicit_manifest_acceptance(app, client, actors):
    owner=actors['owner'];_,version=create_dataset(client,owner,modality='image')
    raw=png();batch=create_import(client,owner,version,raw,count=2)
    item,_=register(client,batch,raw);transfer(client,batch,item,raw)
    root='/api/v1/imports/'+batch['id']
    assert client.post(root+'/cancel',headers=owner['headers']).status_code==200
    assert client.post('/api/v1/versions/'+version['id']+'/submit',headers=owner['headers']).status_code==409
    payload={'confirmed':True,'reason':'业务确认仅接收当前可用子集','expected_clean_files':1}
    assert client.post(root+'/accept-partial',headers=actors['stranger']['headers'],json=payload).status_code==404
    wrong={**payload,'expected_clean_files':2}
    assert client.post(root+'/accept-partial',headers=owner['headers'],json=wrong).status_code==409
    response=client.post(root+'/accept-partial',headers=owner['headers'],json=payload)
    assert response.status_code==200,response.text
    assert response.json()['resolution']['omitted_files']==1
    assert client.post('/api/v1/versions/'+version['id']+'/submit',headers=owner['headers']).status_code==200


def test_organization_quality_policy_and_non_downgradable_tier(client,actors):
    owner=actors['owner'];_,v=create_dataset(client,owner)
    policy={'revision':0,'minimum_tier':'certified'}
    path='/api/v1/spaces/'+owner['space_id']+'/quality-policy'
    assert client.put(path,headers=actors['stranger']['headers'],json=policy).status_code==403
    assert client.put(path,headers=owner['headers'],json=policy).status_code==200
    assert client.put(path,headers=owner['headers'],json=policy).status_code==409
    upload(client,owner,v)
    assert client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==409
    data={'license':v['license'],'source':v['source'],'quality_tier':'certified'}
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json=data).status_code==200
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json={**data,'quality_tier':'raw'}).status_code==409
    assert client.put('/api/v1/governance/versions/'+v['id']+'/quality-policy',headers=owner['headers'],json={'required':False}).status_code==409
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json={**data,'card':{'_source_approval':{'approved':True}}}).status_code==422


def test_curated_delivery_is_server_generated_and_authorized(app,client,actors,monkeypatch):
    owner,first,second,batch,v=submitted_review(app,client,actors,2)
    finish_images(client,first,second,batch)
    update={'license':v['license'],'source':v['source'],'quality_tier':'curated'}
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json=update).status_code==200
    assert client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==409
    dedup=queue(client,owner,v,'dedup',near_distance=0)
    with app.state.db.Session() as db:report=db.get(GovernanceReport,dedup['result']['report_id']);pairs=report.summary['pairs']
    # Distinct synthetic colors share a dHash. Reviewer explicitly rejects these near candidates.
    result=client.post('/api/v1/governance/reports/'+dedup['result']['report_id']+'/decision',headers=second['headers'],
        json={'approve':True,'reviewed_pairs':[p['id'] for p in pairs],'duplicate_pairs':[]})
    assert result.status_code==200,result.text
    build=queue(client,owner,v,'release')
    path='/api/v1/jobs/'+build['id']+'/download'
    response=client.get(path,headers=owner['headers'])
    assert response.status_code==200,response.text
    assert hashlib.sha256(response.content).hexdigest()==build['result']['sha256']
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        labels=json.loads(archive.read('annotations.json'))
        delivery=json.loads(archive.read('delivery.json'))
        assert len(labels['images'])==2 and len(labels['annotations'])==2
        assert not any('truth' in name or 'image_pilot_report' in name for name in archive.namelist())
        for checksum in delivery['checksums']:
            assert hashlib.sha256(archive.read(checksum['path'])).hexdigest()==checksum['sha256']
        assert delivery['contract']['annotation_manifest']
        assert all(image['file_name'].endswith('.png') for image in labels['images'])
    assert client.get(path,headers=actors['stranger']['headers']).status_code==403
    publish(client,owner,first,v)
    def no_whole_version_rescan(*args,**kwargs):
        raise AssertionError('published download must not rescan all annotations')
    monkeypatch.setattr('app.delivery.annotation_snapshot',no_whole_version_rescan)
    assert client.get(path,headers=owner['headers']).status_code==200
    target='/api/v1/deliveries/versions/'+v['id']+'/download'
    assert client.get(target,headers=second['headers']).status_code==403
    approve(client,owner,second,v)
    assert client.post('/api/v1/versions/'+v['id']+'/accept-license',headers=second['headers']).status_code==200
    assert client.get(target,headers=second['headers']).status_code==200


def test_other_batch_edit_invalidates_whole_version_evaluation(app,client,actors):
    owner,first,v,images,source_id=setup_images(app,client,actors,4)
    second=actors['reader']
    with app.state.db.Session() as db:
        source=db.get(Asset,source_id)
        report=json.loads(b''.join(app.state.storage.chunks(source.object_key)))
    batches=[]
    for index in range(2):
        raw=json.dumps({**report,'images':report['images'][index*2:index*2+2]}).encode()
        batches.append(import_batch(client,owner,upload(client,owner,v,raw,f'shard-{index}.json')[1]['asset_id']))
    assert client.post('/api/v1/image-reviews/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==200
    first_batch,other_batch=batches
    detail=client.get('/api/v1/image-reviews/'+first_batch['id'],headers=first['headers']).json()
    item=detail['items'][1]
    negative={**item['annotation'],'labels':{**item['annotation']['labels'],'hat':'no_hat','sleeve':'long_sleeves'}}
    assert client.put('/api/v1/image-reviews/items/'+item['id'],headers=first['headers'],json={
        'revision':item['revision'],'claim_token':claim_token(client,first,item),'annotation':negative}).status_code==200
    for batch in batches:finish_images(client,first,second,batch)
    detail=client.get('/api/v1/image-reviews/'+first_batch['id'],headers=owner['headers']).json()
    truth={'schema_version':1,'images':[{'asset_id':i['asset_id'],'persons':[i['annotation']]} for i in detail['items']]}
    truth_id=upload(client,owner,v,json.dumps(truth).encode(),'truth.json')[1]['asset_id']
    cfg=app.state.settings;cfg.quality_min_truth_images=2;cfg.quality_min_positives=1;cfg.quality_min_negatives=1
    evaluated=queue(client,owner,v,'evaluate',truth_asset_id=truth_id,batch_id=first_batch['id'],evaluation_target='approved_annotations')
    result=client.post('/api/v1/governance/reports/'+evaluated['result']['report_id']+'/decision',headers=second['headers'],
                       json={'approve':True,'truth_verified':True})
    assert result.status_code==200 and result.json()['summary']['quality_gate']['passed'],result.text
    client.put('/api/v1/governance/versions/'+v['id']+'/quality-policy',headers=owner['headers'],json={'required':True})
    other=client.get('/api/v1/image-reviews/'+other_batch['id'],headers=first['headers']).json()['images'][0]
    path='/api/v1/image-reviews/images/'+other['id']
    assert client.post(path+'/reopen',headers=first['headers'],json={'revision':other['revision']}).status_code==200
    claim=client.post(path+'/claim',headers=first['headers']).json()
    persons=[{'item_id':i['id'],'annotation':{**i['annotation'],'labels':{**i['annotation']['labels'],'hat':'no_hat'}}}
             for i in claim['items'] if i['status']!='excluded']
    assert client.put(path,headers=first['headers'],json={'revision':claim['revision'],'claim_token':claim['claim_token'],'persons':persons}).status_code==200
    claim=client.post(path+'/claim',headers=second['headers']).json()
    assert client.post(path+'/approve',headers=second['headers'],json={'revision':claim['revision'],'claim_token':claim['claim_token']}).status_code==200
    rejected=client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers'])
    assert rejected.status_code==409 and '评测报告' in rejected.text,rejected.text


def test_certified_release_requires_source_split_and_policy_bound_evaluation(app,client,actors,monkeypatch):
    owner,first,second,batch,v=submitted_review(app,client,actors,3)
    finish_images(client,first,second,batch)
    data={'license':v['license'],'source':v['source'],'quality_tier':'certified'}
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json=data).status_code==200
    cfg=app.state.settings
    cfg.quality_min_truth_images=2;cfg.quality_min_positives=1;cfg.quality_min_negatives=0
    dedup=queue(client,owner,v,'dedup',near_distance=0)
    with app.state.db.Session() as db:
        pairs=db.get(GovernanceReport,dedup['result']['report_id']).summary['pairs']
    assert client.post('/api/v1/governance/reports/'+dedup['result']['report_id']+'/decision',headers=second['headers'],
        json={'approve':True,'duplicate_pairs':[],'reviewed_pairs':[p['id'] for p in pairs]}).status_code==200
    build_payload={'version_id':v['id'],'kind':'release','idempotency_key':'certified-build-before-split'}
    assert client.post('/api/v1/jobs',headers=owner['headers'],json=build_payload).status_code==409
    images=[p['asset_id'] for p in batch['images']]
    split=queue(client,owner,v,'split',dedup_report_id=dedup['result']['report_id'],groups={a:a for a in images})
    assert client.post('/api/v1/governance/reports/'+split['result']['report_id']+'/decision',headers=second['headers'],json={'approve':True}).status_code==200
    detail=client.get('/api/v1/image-reviews/'+batch['id'],headers=owner['headers']).json()
    truth={'schema_version':1,'images':[{'asset_id':i['asset_id'],'persons':[i['annotation']]} for i in detail['items']]}
    truth_id=upload(client,owner,v,json.dumps(truth).encode(),'cert-truth.json')[1]['asset_id']
    evaluated=queue(client,owner,v,'evaluate',truth_asset_id=truth_id,batch_id=batch['id'],evaluation_target='approved_annotations')
    assert client.post('/api/v1/governance/reports/'+evaluated['result']['report_id']+'/decision',headers=second['headers'],
        json={'approve':True,'truth_verified':True}).json()['summary']['quality_gate']['passed']
    queue(client,owner,v,'release')
    yolo=queue(client,owner,v,'release',delivery_format='yolo')
    raw_yolo=client.get('/api/v1/jobs/'+yolo['id']+'/download',headers=owner['headers'])
    with zipfile.ZipFile(io.BytesIO(raw_yolo.content)) as archive:
        assert 'train: train.txt' in archive.read('dataset.yaml').decode()
        for partition in ('train','val','test'):
            for filename in archive.read(partition+'.txt').decode().splitlines():assert filename.removeprefix('./') in archive.namelist()
        for name in archive.namelist():
            if name.startswith('labels/'):
                for line in archive.read(name).decode().splitlines():assert all(0<=float(value)<=1 for value in line.split()[1:])
    vlm=queue(client,owner,v,'release',delivery_format='vlm-jsonl')
    same=queue(client,owner,v,'release',delivery_format='vlm-jsonl')
    assert vlm['result']['sha256']==same['result']['sha256']
    assert client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==409
    source={'approved':True,'evidence_reference':'synthetic-license-archive','reason':'合成来源与测试许可独立核验'}
    path='/api/v1/governance/versions/'+v['id']+'/source-decision'
    assert client.post(path,headers=owner['headers'],json=source).status_code==403
    assert client.post(path,headers=second['headers'],json=source).status_code==200
    publish(client,owner,first,v)
    raw=client.get('/api/v1/deliveries/versions/'+v['id']+'/download',headers=owner['headers'])
    assert raw.status_code==200
    with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
        payload=json.loads(archive.read('annotations.json'))
        assert {image['split'] for image in payload['images']}=={'train','val','test'}
        contract=json.loads(archive.read('delivery.json'))['contract']
        assert contract['quality_evidence']['report_id']==evaluated['result']['report_id']
        assert 'hat' in contract['quality_evidence']['quality_gate']['coverage']
        assert 'upper_color' not in contract['quality_evidence']['quality_gate']['coverage']
        assert 'dataset_info.json' in archive.namelist()
        sample=json.loads(archive.read('train.jsonl').decode().splitlines()[0])
        assert sample['messages'][0]['content'].count('<image>')==len(sample['images'])==1
        assert json.loads(sample['messages'][1]['content'])['persons']
    with app.state.db.Session() as db:
        assert db.get(Version,v['id']).card['_release_build_id']==same['id']
    def changed_generator(*args,**kwargs):raise RuntimeError('synthetic later generator revision')
    monkeypatch.setattr('app.delivery.release_contract',changed_generator)
    assert client.get('/api/v1/deliveries/versions/'+v['id']+'/download',headers=owner['headers']).status_code==200
    public=client.get('/api/v1/datasets/'+v['dataset_id'],headers=first['headers']).json()
    assert '_source_approval' not in public['versions'][0]['card']


def test_failed_release_retry_re_reserves_space_and_drops_failed_artifacts(app,client,actors,monkeypatch):
    owner,first,second,batch,v=submitted_review(app,client,actors,1)
    finish_images(client,first,second,batch)
    original=app.state.storage.put_file
    def fail(*args,**kwargs):original(*args,**kwargs);raise OSError('synthetic lost acknowledgement')
    monkeypatch.setattr(app.state.storage,'put_file',fail)
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={
        'version_id':v['id'],'kind':'release','idempotency_key':'release-retry-fixture'})
    assert response.status_code==201 and response.json()['status']=='failed'
    job_id=response.json()['id']
    monkeypatch.setattr(app.state.storage,'put_file',original)
    response=client.post('/api/v1/jobs/'+job_id+'/retry',headers=owner['headers'])
    assert response.status_code==200,response.text
    job=client.get('/api/v1/jobs/'+job_id,headers=owner['headers']).json()
    assert job['status']=='completed' and job['reserved_bytes']==0,job


def test_delivery_admission_limits_before_reserving_quota(app,client,actors):
    owner,first,second,batch,v=submitted_review(app,client,actors,2)
    finish_images(client,first,second,batch)
    app.state.settings.delivery_max_images=1
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={
        'version_id':v['id'],'kind':'release','idempotency_key':'capacity-boundary-test'})
    assert response.status_code==413,response.text
    from app.models import Space,Dataset
    with app.state.db.Session() as db:
        dataset=db.get(Dataset,v['dataset_id'])
        assert db.get(Space,dataset.space_id).reserved_bytes==0


def test_delivery_exif_orientation_matches_the_review_coordinate_system(app,client,actors):
    from PIL import Image
    owner,first,v,_,report_id=setup_images(app,client,actors,1)
    second=actors['reader']
    with app.state.db.Session() as db:
        source=db.get(Asset,report_id)
        report=json.loads(b''.join(app.state.storage.chunks(source.object_key)))
    out=io.BytesIO();original=Image.new('RGB',(80,64),'green');exif=Image.Exif();exif[274]=6
    original.save(out,'JPEG',exif=exif);original.close()
    portrait_id=upload(client,owner,v,out.getvalue(),'rotated.jpg')[1]['asset_id']
    portrait=json.loads(json.dumps(report['images'][0]));portrait['asset_id']=portrait_id
    report['images'].append(portrait)
    report_id=upload(client,owner,v,json.dumps(report).encode(),'orientation-report.json')[1]['asset_id']
    batch=import_batch(client,owner,report_id)
    assert client.post('/api/v1/image-reviews/'+batch['id']+'/submit',headers=owner['headers']).status_code==200
    finish_images(client,first,second,batch)
    build=queue(client,owner,v,'release')
    raw=client.get('/api/v1/jobs/'+build['id']+'/download',headers=owner['headers'])
    assert raw.status_code==200
    with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
        payload=json.loads(archive.read('annotations.json'))
        entry=next(image for image in payload['images'] if image['source_asset_id']==portrait_id)
        assert (entry['width'],entry['height'])==(64,80)
        with Image.open(io.BytesIO(archive.read(entry['file_name']))) as normalized:
            assert normalized.size==(64,80) and normalized.getexif().get(274,1)==1
