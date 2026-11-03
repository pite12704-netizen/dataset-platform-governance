"""摘要负载、真实进度、查询退避契约；所有数据只在隔离测试库。"""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event,select,update

from app.models import Asset,Job,Membership
from app.tasks import input_assets,run_job
from test_lifecycle import create_dataset,upload


def test_version_and_assistant_quality_use_small_latest_id_and_summary_projection(client,actors,app):
    from datetime import timedelta
    from app.db import now
    from app.models import QualityReport,User
    from app.assistant_tools import quality_info
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        for index in range(2):
            job=Job(space_id=owner['space_id'],user_id=owner['id'],version_id=version['id'],kind='quality',
                parameters={},result={},status='completed',idempotency_key='large-quality-'+str(index))
            db.add(job);db.flush()
            db.add(QualityReport(version_id=version['id'],job_id=job.id,created_at=now()-timedelta(days=1-index),
                summary={'method':'rules-v1','model':None,'total':16801,'checked':16801,'passed':16801,
                    'failed':0,'duplicate_files':index,'not_checked':['semantic'],
                    'assets':{'private-details':'x'*(6*1024**2)}}))
        db.commit()
    statements=[]
    def observe(conn,cursor,statement,parameters,context,many):
        if 'quality_reports' in statement:statements.append(statement.lower())
    def reject_full_report(*args):raise AssertionError('Summary paths must not load full report entities')
    event.listen(app.state.db.engine,'before_cursor_execute',observe);event.listen(QualityReport,'load',reject_full_report)
    try:
        result=client.get('/api/v1/versions/'+version['id'],headers=owner['headers'])
        assert result.status_code==200,result.text
        quality=result.json()['quality'];assert quality['duplicate_files']==1 and quality['checked']==16801
        assert 'assets' not in quality and len(result.content)<20000
        with app.state.db.Session() as db:
            info=quality_info(db,db.get(User,owner['id']),app.state.settings,version['id'])
            assert info['duplicate_files']==1 and info['status']=='available'
        assert all('summary' not in q for q in statements if 'order by' in q)
        assert all('limit' in q for q in statements if 'order by' in q)
        denied=client.get('/api/v1/versions/'+version['id'],headers=actors['stranger']['headers'])
        assert denied.status_code==404
    finally:
        event.remove(app.state.db.engine,'before_cursor_execute',observe);event.remove(QualityReport,'load',reject_full_report)


def test_compact_jobs_does_not_load_large_job_entities_or_return_input_manifest(client,actors,app):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        for index in range(2):
            db.add(Job(space_id=owner['space_id'],user_id=owner['id'],version_id=version['id'],kind='quality',status='running',
                parameters={'input_assets':[{'id':str(n),'sha256':'f'*64,'size':1} for n in range(2000)]},
                result={'total':2000,'phase':'checking','processed_files':5,'total_files':2000,'assets':{'large':'do-not-return'*10000}},
                idempotency_key='compact-'+str(index)))
        db.commit()
    def reject_job_load(*args):raise AssertionError('Compact list must not hydrate the full Job JSON entity')
    event.listen(Job,'load',reject_job_load)
    try:
        response=client.get('/api/v1/jobs?compact=true&limit=1',headers=owner['headers'])
        assert response.status_code==200,response.text
        body=response.json();assert len(body['items'])==1 and body['has_more'] and body['next_offset']==1
        assert body['items'][0]['result']['processed_files']==5
        assert 'parameters' not in body['items'][0] and 'assets' not in body['items'][0]['result']
        assert len(response.content)<4000 and 'do-not-return' not in response.text
        assert client.get('/api/v1/jobs?compact=true',headers=actors['stranger']['headers']).json()['items']==[]
        with app.state.db.Session() as db:
            member=db.scalar(select(Membership).where(Membership.user_id==owner['id'],Membership.space_id==owner['space_id']))
            member.active=False;db.commit()
        assert client.get('/api/v1/jobs?compact=true',headers=owner['headers']).json()['items']==[]
    finally:event.remove(Job,'load',reject_job_load)


def test_input_snapshot_uses_batched_queries_and_still_rejects_changed_bytes(client,actors,app):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    manifest=[]
    with app.state.db.Session() as db:
        for index in range(1001):
            identifier=uuid4().hex;manifest.append({'id':identifier,'size':1,'sha256':'a'*64})
            db.add(Asset(id=identifier,version_id=version['id'],filename=f'{index}.png',object_key='assets/'+identifier,
                size=1,sha256='a'*64,status='clean'))
        db.commit()
    selected=[]
    def observe(conn,cursor,statement,*args):
        if statement.lstrip().upper().startswith('SELECT') and 'FROM assets' in statement:selected.append(statement)
    event.listen(app.state.db.engine,'before_cursor_execute',observe)
    try:
        with app.state.db.Session() as db:
            snapshot=SimpleNamespace(kind='quality',version_id=version['id'],parameters={'input_assets':list(reversed(manifest))})
            assets=input_assets(db,snapshot)
            assert [asset.id for asset in assets]==[row['id'] for row in reversed(manifest)]
            assert len(selected)==3
            db.execute(update(Asset).where(Asset.id==manifest[0]['id']).values(size=2));db.commit()
            with pytest.raises(HTTPException) as error:input_assets(db,snapshot)
            assert error.value.status_code==409
    finally:event.remove(app.state.db.engine,'before_cursor_execute',observe)


def test_quality_progress_and_processed_count_advance_before_completion(client,actors,app,monkeypatch):
    import app.tasks as tasks
    owner=actors['owner'];_,version=create_dataset(client,owner)
    for index in range(3):upload(client,owner,version,b'a,b\n1,2\n',f'{index}.csv')
    app.state.settings.task_mode='manual'
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':version['id'],'kind':'quality','idempotency_key':'progress-check'})
    assert response.status_code==201,response.text
    identifier=response.json()['id'];samples=[]
    original=tasks.inspect_asset
    clock=[0]
    def monotonic():clock[0]+=3;return clock[0]
    monkeypatch.setattr(tasks,'time',SimpleNamespace(monotonic=monotonic))
    def inspect(asset,storage,settings,heartbeat):
        with app.state.db.Session() as db:
            current=db.get(Job,identifier);samples.append((current.progress,current.result.copy()))
        return original(asset,storage,settings,heartbeat)
    monkeypatch.setattr(tasks,'inspect_asset',inspect)
    run_job(identifier,app.state.db,app.state.storage,app.state.settings)
    assert [result['processed_files'] for _,result in samples]==[0,1,2]
    assert all(result['total_files']==3 for _,result in samples)
    assert 0<samples[0][0]<samples[1][0]<samples[2][0]<100
    with app.state.db.Session() as db:
        job=db.get(Job,identifier);assert job.status=='completed' and job.progress==100 and job.result['total']==3
