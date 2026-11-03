"""Dataset modality is enforced before object allocation and under the write lock."""
import hashlib,uuid,io
import pytest
from PIL import Image
from fastapi import HTTPException
from sqlalchemy import select,func
from app.checks import require_modality,MODALITY_FORMATS
from app.models import Dataset,Space,Upload
from test_lifecycle import create_dataset,upload

@pytest.mark.parametrize('modality,filename,allowed',[
    ('text','PHOTO.JPG',False),('text','corpus.jsonl',True),('text','document.pdf',True),
    ('image','annotations.json',True),('image','labels.txt',True),('image','photo.webp',True),
    ('image','movie.mp4',False),('audio','sample.mp3',True),('audio','photo.png',False),
    ('video','sample.webm',True),('tabular','data.csv',True),('tabular','photo.jpg',False),
    ('multimodal','photo.jpg',True),('multimodal','payload.exe',False)])
def test_format_policy(modality,filename,allowed):
    if allowed:require_modality(modality,filename)
    else:
        with pytest.raises(HTTPException) as exc:require_modality(modality,filename)
        assert exc.value.status_code==422

def test_default_text_rejects_images_before_allocating_storage_or_quota(app,client,actors,monkeypatch):
    owner=actors['owner'];headers=owner['headers']
    ds=client.post('/api/v1/datasets',headers=headers,json={'name':'Default text','space_id':owner['space_id']}).json()
    assert ds['modality']=='text'
    v=client.post('/api/v1/datasets/'+ds['id']+'/versions',headers=headers,json={'license':'test','source':'synthetic'}).json()
    def unexpected(*args):raise AssertionError('Invalid modality must not allocate an object')
    with monkeypatch.context() as patch:
        patch.setattr(app.state.storage,'initiate',unexpected)
        r=client.post('/api/v1/versions/'+v['id']+'/uploads',headers=headers,json={'filename':'photo.png','size':3,'sha256':hashlib.sha256(b'png').hexdigest(),'idempotency_key':uuid.uuid4().hex})
        assert r.status_code==422 and '文本' in r.text
    with app.state.db.Session() as db:
        assert db.get(Space,owner['space_id']).reserved_bytes==0
        assert db.scalar(select(func.count()).select_from(Upload))==0
    r=client.post('/api/v1/imports',headers=headers,json={'version_id':v['id'],'source_id':'synthetic','expected_files':1,'expected_bytes':3,'idempotency_key':uuid.uuid4().hex})
    assert r.status_code==422
    upload(client,owner,v,b'hello','corpus.txt')
    formats=client.get('/api/v1/config').json()['modality_formats']
    assert formats['text']==sorted(MODALITY_FORMATS['text'])

def test_modality_cannot_change_to_bypass_upload_or_conflict_with_existing_assets(app,client,actors):
    owner=actors['owner'];ds,v=create_dataset(client,owner,modality='image')
    upload(client,owner,v,b'{"labels":[]}','labels.json')
    out=io.BytesIO();Image.new('RGB',(16,16),'blue').save(out,'PNG')
    upload(client,owner,v,out.getvalue(),'photo.png')
    body={'space_id':ds['space_id'],'name':ds['name'],'modality':'text'}
    assert client.put('/api/v1/datasets/'+ds['id'],headers=owner['headers'],json=body).status_code==422
    # A historical wrong label may be explicitly corrected; no files are rewritten.
    with app.state.db.Session() as db:
        db.get(Dataset,ds['id']).modality='text';db.commit()
    body['modality']='image'
    path='/api/v1/datasets/'+ds['id']+'/modality'
    assert client.put(path,headers=actors['stranger']['headers'],json={'modality':'image'}).status_code==403
    assert client.put(path,headers=owner['headers'],json={'modality':'image'}).status_code==200
    assert client.put(path,headers=owner['headers'],json={'modality':'text'}).status_code==422

def test_write_phase_rechecks_type_after_storage_preparation(app,client,actors):
    from types import SimpleNamespace
    from app.uploads import prepare_storage,create_upload,cleanup_prepared
    from app.schemas import UploadInput
    from app.models import User
    owner=actors['owner'];ds,v=create_dataset(client,owner,modality='image')
    data=UploadInput(filename='photo.png',size=3,sha256='a'*64,idempotency_key=uuid.uuid4().hex)
    request=SimpleNamespace(app=app)
    with app.state.db.Session() as db:
        user=db.get(User,owner['id']);prepared=prepare_storage(v['id'],data,request,db,user)
        db.get(Dataset,ds['id']).modality='text';db.commit()
        try:
            with pytest.raises(HTTPException) as exc:create_upload(v['id'],data,request,db,user,storage_session=prepared)
            assert exc.value.status_code==422
            db.rollback()
        finally:cleanup_prepared(db,request,prepared)
