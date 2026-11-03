from sqlalchemy import select
from app.models import Thumbnail,Space,Version
from test_image_reviews import setup_images


def test_private_persistent_thumbnail_survives_memory_cache_and_is_counted(app,client,actors,monkeypatch):
    owner,_,v,assets,_=setup_images(app,client,actors,1)
    response=client.post('/api/v1/admin/thumbnails/prewarm',headers=actors['admin']['headers'],json={'version_id':v['id']})
    assert response.status_code==200 and len(response.json()['job_ids'])==1,response.text
    with app.state.db.Session() as db:
        rows=db.scalars(select(Thumbnail).where(Thumbnail.asset_id==assets[0])).all()
        assert len(rows)==2 and all(row.size>0 for row in rows)
    app.state.preview_cache.entries.clear();app.state.preview_cache.bytes=0
    original=app.state.storage.chunks
    def chunks(key,*args,**kwargs):
        assert key.startswith('thumbnails/'),'Preview must use persistent derivative'
        return original(key,*args,**kwargs)
    monkeypatch.setattr(app.state.storage,'chunks',chunks)
    assert client.get('/api/v1/assets/'+assets[0]+'/preview',headers=owner['headers']).status_code==200
    assert client.get('/api/v1/assets/'+assets[0]+'/preview',headers=actors['stranger']['headers']).status_code==403


def test_monitoring_scope_and_bounded_request_labels(app,client,actors):
    client.get('/api/v1/not-a-route/private-account-12345',headers=actors['owner']['headers'])
    assert client.get('/api/v1/admin/metrics',headers=actors['owner']['headers']).status_code==403
    assert client.post('/api/v1/credentials',headers=actors['owner']['headers'],json={'name':'bad','scopes':['metrics:read']}).status_code==403
    credential=client.post('/api/v1/credentials',headers=actors['admin']['headers'],json={'name':'scraper','scopes':['metrics:read']})
    assert credential.status_code==201
    token={'Authorization':'Bearer '+credential.json()['token']}
    report=client.get('/api/v1/admin/metrics',headers=token)
    assert report.status_code==200 and 'platform_http_requests_total' in report.text
    assert 'private-account-12345' not in report.text and 'owner@example' not in report.text
    assert client.get('/api/v1/datasets',headers=token).status_code==403
    summary=client.get('/api/v1/admin/health-summary',headers=actors['admin']['headers'])
    assert summary.status_code==200 and summary.json()['scope']=='shared-database'


def test_heartbeat_namespace_is_shared_by_replicas_but_not_other_instances(app):
    from app.operations import presence_key
    cfg=app.state.settings.model_copy(update={'database_url':'mysql://mysql:3306/test_schema'})
    assert presence_key(cfg,'worker')==presence_key(cfg.model_copy(update={'database_url':'mysql://127.0.0.1:13306/test_schema'}),'worker')
    assert presence_key(cfg,'worker')!=presence_key(cfg.model_copy(update={'secret_key':'another-test-instance-key-0123456789'}),'worker')
