import io,json,zipfile,uuid
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from app.models import ImageReviewBatch,ImageReviewItem,Job,Version
from app.review_templates import digest
from app.image_reviews import checked_annotation
from app.export_formats import extra_files
from test_image_reviews import setup_images,finish_images,import_batch
from test_lifecycle import upload

TEMPLATE={'schema_version':1,'name':'车辆检查','classes':[{'key':'car','label':'轿车'},{'key':'truck','label':'货车'}],
    'fields':[{'key':'color','label':'颜色','type':'enum','options':['red','blue']},
        {'key':'damaged','label':'破损','type':'boolean'},{'key':'count','label':'数量','type':'number'},
        {'key':'note','label':'说明','type':'text'}]}

def bind(client,owner,version):
    response=client.put('/api/v1/review-templates/versions/'+version['id'],headers=owner['headers'],json=TEMPLATE)
    assert response.status_code==200,response.text
    return response.json()['template']

def annotation(category='car'):
    return {'bbox':[4,5,30,60],'category':category,'labels':{'color':'red','damaged':False,'count':0,'note':None}}

def test_template_validation_and_owner_only_binding(client,actors,app):
    owner,_,v,_,_=setup_images(app,client,actors)
    path='/api/v1/review-templates/versions/'+v['id']
    assert client.put(path,headers=actors['stranger']['headers'],json=TEMPLATE).status_code==403
    bad={**TEMPLATE,'classes':[TEMPLATE['classes'][0],TEMPLATE['classes'][0]]}
    assert client.put(path,headers=owner['headers'],json=bad).status_code==422
    template=bind(client,owner,v)
    assert checked_annotation(annotation(),64,80,template)==annotation()
    for value in [{**annotation(),'category':'dog'}, {**annotation(),'labels':{'color':'red'}},
                  {**annotation(),'labels':{**annotation()['labels'],'damaged':'false'}},
                  {**annotation(),'labels':{**annotation()['labels'],'count':True}}]:
        with pytest.raises(HTTPException):checked_annotation(value,64,80,template)
    with pytest.raises(HTTPException):checked_annotation(annotation(),64,80)
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json={'source':'s','license':'l','card':{'_review_template':TEMPLATE}}).status_code==422

def test_generic_import_review_independence_and_snapshot_freeze(client,actors,app):
    owner,first,v,images,_=setup_images(app,client,actors)
    template=bind(client,owner,v);second=actors['reader']
    report={'schema_version':2,'template_digest':digest(template),'images':[{'asset_id':images[0],
        'width':64,'height':80,'objects':[{'id':'1',**annotation('truck')}]}]}
    aid=upload(client,owner,v,json.dumps(report).encode(),'generic-report.json')[1]['asset_id']
    batch=import_batch(client,owner,aid)
    assert batch['items'][0]['annotation']['category']=='truck'
    assert batch['provenance']['template_digest']==digest(template)
    assert client.put('/api/v1/review-templates/versions/'+v['id'],headers=owner['headers'],json=TEMPLATE).status_code==409
    assert client.post('/api/v1/image-reviews/'+batch['id']+'/submit',headers=owner['headers']).status_code==200
    finish_images(client,first,second,batch)
    exported=client.get('/api/v1/image-reviews/'+batch['id']+'/export',headers=owner['headers'])
    assert exported.status_code==200,exported.text
    assert exported.json()['annotations'][0]['annotation']==annotation('truck')
    assert exported.json()['kind']=='approved_object_annotations'
    with app.state.db.Session() as db:
        item=db.scalar(select(ImageReviewItem).where(ImageReviewItem.batch_id==batch['id']))
        assert item.predicted==annotation('truck') and item.reviewer_id!=item.editor_id
    build=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':v['id'],'kind':'release',
        'delivery_format':'coco','idempotency_key':uuid.uuid4().hex})
    assert build.status_code==201,build.text
    assert build.json()['status']=='completed',build.json().get('error')
    archive=client.get('/api/v1/jobs/'+build.json()['id']+'/download',headers=owner['headers'])
    assert archive.status_code==200,archive.text
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        payload=json.loads(package.read('annotations.json'))
        assert payload['categories'][1]['name']=='truck'
        assert payload['annotations'][0]['category_id']==2
        assert payload['review_template']==template

def test_generic_worker_schema_checkpoint_and_template_fingerprint(client,actors,app,monkeypatch):
    from app.vision import fingerprint
    owner,_,v,images,_=setup_images(app,client,actors)
    template=bind(client,owner,v)
    app.state.settings.model_url='http://127.0.0.1:1234/v1';app.state.settings.model_name='synthetic';app.state.settings.vision_enabled=True
    assert fingerprint(app.state.settings,template)!=fingerprint(app.state.settings)
    def model(image,settings,prompt,schema,name,max_tokens=3500):
        assert schema['properties']['objects']['items']['properties']['category']['enum']==['car','truck']
        assert 'vehicles' not in prompt # configured labels are data, not a fixed domain prompt
        return {'objects':[{'category':'truck','bbox':[0,0,500,500],'labels':annotation()['labels']}],'truncated':False},{'seconds':0,'usage':{}}
    monkeypatch.setattr('app.vision.call_image',model)
    result=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':v['id'],'kind':'vision','idempotency_key':uuid.uuid4().hex})
    assert result.status_code==201,result.text
    assert result.json()['status']=='completed',result.json().get('error')
    with app.state.db.Session() as db:
        b=db.scalar(select(ImageReviewBatch).where(ImageReviewBatch.job_id==result.json()['id']))
        row=db.scalar(select(ImageReviewItem).where(ImageReviewItem.batch_id==b.id))
        assert row.annotation['category']=='truck' and b.provenance['template_digest']==digest(template)

def test_yolo_and_vlm_export_use_generic_class_mapping():
    payload={'review_template':TEMPLATE,'categories':[{'id':1,'name':'car'},{'id':2,'name':'truck'}],
        'images':[{'id':1,'source_asset_id':'asset1','split':'train','file_name':'images/asset1.png','width':64,'height':80}],
        'annotations':[{'image_id':1,'category_id':2,'bbox':[4,5,30,60],'attributes':annotation()['labels']}]}
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:extra_files(archive,payload,'yolo',[]);extra_files(archive,payload,'vlm-jsonl',[])
    with zipfile.ZipFile(stream) as archive:
        assert archive.read('labels/asset1.txt').startswith(b'1 ')
        assert '1: truck' in archive.read('dataset.yaml').decode()
        line=json.loads(archive.read('train.jsonl'))
        assert json.loads(line['messages'][1]['content'])['objects'][0]['category']=='truck'


def test_generic_no_target_requires_two_reviewers_and_cannot_claim_person_certification(client,actors,app,monkeypatch):
    owner,first,v,_,_=setup_images(app,client,actors);bind(client,owner,v)
    app.state.settings.model_url='http://127.0.0.1:1234/v1';app.state.settings.model_name='synthetic';app.state.settings.vision_enabled=True
    monkeypatch.setattr('app.vision.call_image',lambda *args,**kwargs:({'objects':[],'truncated':False},{'seconds':0,'usage':{}}))
    job=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':v['id'],'kind':'vision','idempotency_key':uuid.uuid4().hex})
    assert job.status_code==201 and job.json()['status']=='completed'
    with app.state.db.Session() as db:b=db.scalar(select(ImageReviewBatch).where(ImageReviewBatch.job_id==job.json()['id']));identifier=b.id
    batch=client.get('/api/v1/image-reviews/'+identifier,headers=owner['headers']).json()
    assert batch['items']==[] and batch['images'][0]['status']=='pending'
    assert client.get('/api/v1/image-reviews/'+identifier+'/export',headers=owner['headers']).status_code==409
    assert client.post('/api/v1/image-reviews/'+identifier+'/submit',headers=owner['headers']).status_code==200
    finish_images(client,first,actors['reader'],batch)
    result=client.get('/api/v1/image-reviews/'+identifier+'/export',headers=owner['headers'])
    assert result.status_code==200 and result.json()['images'][0]['no_target'] is True
    assert client.put('/api/v1/versions/'+v['id'],headers=owner['headers'],json={'source':v['source'],'license':v['license'],'quality_tier':'certified'}).status_code==200
    assert client.post('/api/v1/versions/'+v['id']+'/submit',headers=owner['headers']).status_code==409
