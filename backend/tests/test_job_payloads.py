"""Large immutable input/result fields never re-enter lease/progress rows."""
import copy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event,select,text,update

from app.db import now
from app.models import Asset,Job,JobPayload
from app.job_payloads import AUTH,HOT_BYTES,REFS,canonical,hydrate_job,split,unpack
from scripts.platform_data import upgrade_manifest,canonical as backup_canonical
from test_lifecycle import create_dataset


def large_manifest(size=6000):
    return [{'id':f'{number:032x}','sha256':hashlib.sha256(str(number).encode()).hexdigest(),'size':number+1} for number in range(size)]


def test_large_inputs_and_results_are_cold_and_progress_never_updates_payload(client,actors,app):
    owner=actors['owner'];_,version=create_dataset(client,owner);manifest=large_manifest();identifier=uuid4().hex
    with app.state.db.Session() as db:
        db.add(Job(id=identifier,space_id=owner['space_id'],user_id=owner['id'],version_id=version['id'],kind='quality',status='running',
            parameters={'input_assets':manifest},result={'assets':{str(i):{'passed':True} for i in range(3000)},'total':6000},idempotency_key='payload-test'))
        db.commit()
    writes=[]
    def record(conn,cursor,statement,*args):
        if statement.lstrip().upper().startswith(('UPDATE','INSERT')):writes.append(statement.lower())
    event.listen(app.state.db.engine,'before_cursor_execute',record)
    try:
        with app.state.db.Session() as db:
            job=db.get(Job,identifier)
            assert len(canonical(job.parameters))<=HOT_BYTES and REFS in job.parameters
            assert len(canonical(job.result))<=HOT_BYTES and REFS in job.result
            payload_rows=db.scalars(select(JobPayload).where(JobPayload.job_id==identifier)).all()
            assert len(payload_rows)==2
            hydrate_job(db,job);assert job.parameters['input_assets']==manifest and len(job.result['assets'])==3000
            db.commit()  # Read-only hydration must not write the row or re-encode the payload.
            assert writes==[]
            for progress in range(1,12):
                job.progress=progress;db.commit()
                db.expire(job,['parameters','result'])
                assert len(canonical(job.parameters))<=HOT_BYTES and len(canonical(job.result))<=HOT_BYTES
            assert not any('job_payloads' in sql for sql in writes)
        response=client.get('/api/v1/jobs/'+identifier,headers=owner['headers'])
        assert response.status_code==200 and response.json()['parameters']['input_assets']==manifest
        assert len(response.json()['result']['assets'])==3000
        assert client.get('/api/v1/jobs/'+identifier,headers=actors['stranger']['headers']).status_code==404
    finally:event.remove(app.state.db.engine,'before_cursor_execute',record)


def test_references_cannot_cross_tasks_and_payloads_cannot_be_edited(client,actors,app):
    owner=actors['owner'];_,version=create_dataset(client,owner)
    with app.state.db.Session() as db:
        job=Job(space_id=owner['space_id'],user_id=owner['id'],version_id=version['id'],kind='quality',
            parameters={'input_assets':large_manifest(100)},idempotency_key='integrity')
        db.add(job);db.commit();reference=job.parameters[REFS]['input_assets']
        row=db.get(JobPayload,reference['id'])
        with pytest.raises(HTTPException):unpack(row,'f'*32,'parameters','input_assets',reference)
        row.content='changed'
        with pytest.raises(HTTPException,match='不可原地修改'):db.commit()
        db.rollback()


def test_legacy_large_row_is_compacted_before_first_orm_mutation(client,actors,app):
    owner=actors['owner'];_,version=create_dataset(client,owner);identifier=uuid4().hex
    legacy={'id':identifier,'space_id':owner['space_id'],'user_id':owner['id'],'version_id':version['id'],'kind':'quality',
            'parameters':{'input_assets':large_manifest()},'result':{},'status':'queued','idempotency_key':'legacy-big'}
    with app.state.db.engine.begin() as connection:connection.execute(Job.__table__.insert(),legacy)
    with app.state.db.Session() as db:
        job=db.get(Job,identifier);assert len(canonical(job.parameters))>HOT_BYTES
        job.status='running';db.commit();db.refresh(job)
        assert len(canonical(job.parameters))<=HOT_BYTES and REFS in job.parameters
        hydrate_job(db,job);assert job.parameters['input_assets']==legacy['parameters']['input_assets']


def test_old_backup_upgrade_preserves_frozen_inputs_and_rejects_payload_tampering():
    manifest={'schema_version':9,'tables':{'jobs':[{'id':'a'*32,'created_at':now(),'parameters':{'input_assets':large_manifest(100)},'result':{}}]}}
    manifest['metadata_sha256']=hashlib.sha256(backup_canonical(manifest['tables'])).hexdigest()
    result=upgrade_manifest(copy.deepcopy(manifest));assert result['schema_version']==11
    job=result['tables']['jobs'][0];row=result['tables']['job_payloads'][0]
    value=unpack(SimpleNamespace(**row),job['id'],'parameters','input_assets',job['parameters'][REFS]['input_assets'])
    assert value==manifest['tables']['jobs'][0]['parameters']['input_assets']
    broken=copy.deepcopy(result);broken['tables']['job_payloads'][0]['content']='bad'
    broken['metadata_sha256']=hashlib.sha256(backup_canonical(broken['tables'])).hexdigest()
    with pytest.raises(HTTPException):upgrade_manifest(broken)


def test_blind_evaluation_identity_is_preserved_in_hot_projection():
    data={'governance_options':{'evaluation_set_id':'e'*32,'groups':{str(i):'group' for i in range(1000)}}}
    hot,rows=split('a'*32,'parameters',data)
    assert hot[AUTH]=='e'*32 and len(rows)==1 and 'governance_options' in hot[REFS]


def test_detached_failure_cleanup_uses_the_frozen_cold_input_manifest(client,actors,app,monkeypatch):
    import hashlib
    from app.tasks import run_job
    import converter_core
    owner=actors['owner'];_,version=create_dataset(client,owner);raw=b'a,b\n1,2\n'
    with app.state.db.Session() as db:
        for index in range(40):
            identifier=uuid4().hex;key='assets/'+identifier;app.state.storage.put_bytes(key,raw)
            db.add(Asset(id=identifier,version_id=version['id'],filename=f'{index}.csv',object_key=key,
                size=len(raw),sha256=hashlib.sha256(raw).hexdigest(),status='clean'))
        db.commit()
    app.state.settings.task_mode='manual'
    response=client.post('/api/v1/jobs',headers=owner['headers'],json={'version_id':version['id'],'kind':'convert',
        'target_format':'json','idempotency_key':'failed-cold-cleanup'})
    identifier=response.json()['id'];calls=[]
    def convert(*args,**kwargs):
        calls.append(1)
        if len(calls)>1:raise RuntimeError('synthetic conversion failure')
        return SimpleNamespace(success=True,content=b'converted')
    monkeypatch.setattr(converter_core,'convert',convert)
    run_job(identifier,app.state.db,app.state.storage,app.state.settings)
    with app.state.db.Session() as db:
        row=db.get(Job,identifier);assert row.status=='failed'
        assert not list(app.state.settings.storage_root.glob('derived/'+identifier+'/*/*'))


def test_maintenance_compaction_is_read_only_by_default_and_needs_explicit_apply(client,actors,app):
    from scripts.compact_job_payloads import compact
    owner=actors['owner'];_,version=create_dataset(client,owner);identifier=uuid4().hex
    with app.state.db.engine.begin() as connection:
        connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
        connection.execute(text("INSERT INTO alembic_version VALUES ('a85d4e21fa39')"))
        connection.execute(Job.__table__.insert(),{'id':identifier,'space_id':owner['space_id'],'user_id':owner['id'],
            'version_id':version['id'],'kind':'quality','parameters':{'input_assets':large_manifest(100)},
            'result':{},'status':'queued','idempotency_key':'maintenance-preview'})
    preview=compact(app.state.settings)
    assert preview['mode']=='preview' and preview['candidates']==1
    with app.state.db.Session() as db:assert REFS not in db.get(Job,identifier).parameters
    with pytest.raises(ValueError):compact(app.state.settings,apply=True)
    report=compact(app.state.settings,apply=True,quiesced=True)
    assert report['hot_rows_bounded'] and report['jobs_visited']==1 and report['jobs_cancelled']==0
    with app.state.db.Session() as db:
        job=db.get(Job,identifier);assert job.status=='queued' and REFS in job.parameters
