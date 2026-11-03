"""在独立 MySQL 数据库检验整图租约、重复确认和对话并发。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from app.models import ImageReviewImageEvent,AgentRun,Asset,Job,VisionCheckpoint
from test_review_mysql import mysql_case,concurrent_posts
from test_image_reviews import submitted_review
from test_lifecycle import create_dataset
from test_agent_runtime import tool,final


def test_mysql_whole_image_claim_and_duplicate_confirmation_have_one_winner(mysql_case):
    app,client,actors=mysql_case
    owner,first,second,batch,_=submitted_review(app,client,actors,1)
    page=client.get('/api/v1/image-reviews/'+batch['id'],headers=first['headers']).json()['images'][0]
    root='/api/v1/image-reviews/images/'+page['id']
    replies=concurrent_posts(app,[(root+'/claim',first,{}),(root+'/claim',second,{})])
    assert sorted(r.status_code for r in replies)==[200,409],[r.text for r in replies]
    index=next(i for i,r in enumerate(replies) if r.status_code==200)
    editor=[first,second][index];reviewer=[first,second][1-index];claim=replies[index].json()
    result=client.put(root,headers=editor['headers'],json={'revision':0,'claim_token':claim['claim_token'],
        'persons':[{'item_id':i['id'],'annotation':i['annotation']} for i in claim['items']]})
    assert result.status_code==200,result.text
    claim=client.post(root+'/claim',headers=reviewer['headers']).json()
    payload={'revision':1,'claim_token':claim['claim_token']}
    replies=concurrent_posts(app,[(root+'/approve',reviewer,payload)]*2)
    assert sorted(r.status_code for r in replies)==[200,409],[r.text for r in replies]
    with app.state.db.Session() as db:
        events=db.scalars(select(ImageReviewImageEvent).where(ImageReviewImageEvent.image_id==page['id'],ImageReviewImageEvent.action=='approve')).all()
        assert len(events)==1


def test_mysql_same_conversation_blocks_second_turn_without_holding_model_transaction(mysql_case,monkeypatch):
    app,client,actors=mysql_case;owner=actors['owner'];_,version=create_dataset(client,owner)
    first=client.post('/api/v1/agent/chat',headers=owner['headers'],json={'message':'查找','intent':'search'}).json()
    cfg=app.state.settings;cfg.agent_tools_enabled=True;cfg.model_url='http://127.0.0.1:1234/v1';cfg.model_name='scripted'
    arrived,release=Event(),Event();responses=iter([tool(args={'version_id':version['id']}),final([version['id']])])
    def model(*args):
        arrived.set();assert release.wait(10);return next(responses)
    monkeypatch.setattr('app.agent_runtime.async_model_response',model)
    payload={'message':'解释数据','intent':'agent','conversation_id':first['conversation_id'],'request_id':__import__('uuid').uuid4().hex}
    def request():
        from app.main import create_app
        with TestClient(create_app(app.state.settings)) as c:return c.post('/api/v1/agent/chat',headers=owner['headers'],json=payload)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(request)
        try:
            assert arrived.wait(10)
            with app.state.db.Session() as db:
                assert db.scalar(select(AgentRun).where(AgentRun.request_id==payload['request_id'])).status=='running'
            response=client.post('/api/v1/agent/chat',headers=owner['headers'],json={**payload,'request_id':'mysql-second-request'})
            assert response.status_code==409,response.text
            same=client.post('/api/v1/agent/chat',headers=owner['headers'],json=payload)
            assert same.status_code==409
        finally:release.set()
        response=future.result(timeout=15)
    assert response.status_code==200 and response.json()['status']=='completed',response.text


def test_mysql_checkpoint_commit_during_delete_wait_keeps_original(mysql_case):
    import uuid
    from test_image_reviews import setup_images
    app, client, actors = mysql_case
    owner, _, version, images, _ = setup_images(app, client, actors)
    with app.state.db.Session() as db:
        asset = db.get(Asset, images[0])
        original = b''.join(app.state.storage.chunks(asset.object_key))
        space_id = db.scalar(select(Job.space_id).where(Job.version_id == version['id']).limit(1))
        job = Job(space_id=space_id, user_id=owner['id'], version_id=version['id'],
                  kind='vision', status='running', parameters={}, idempotency_key=uuid.uuid4().hex)
        db.add(job); db.commit(); job_id = job.id
    waiting = Event()
    def before_query(conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if 'from jobs' in lowered and 'jobs.version_id' in lowered and 'for update' in lowered:
            waiting.set()
    def request_delete():
        from app.main import create_app
        child = create_app(app.state.settings)
        child.state.db = app.state.db  # 同步 Engine 可在线程间共享，用于观测同一锁检查。
        with TestClient(child) as worker_client:
            return worker_client.delete('/api/v1/assets/' + images[0], headers=owner['headers'])
    event.listen(app.state.db.engine, 'before_cursor_execute', before_query)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with app.state.db.Session() as checkpoint_db:
                # Hold only the job lock, as the vision checkpoint transaction does.
                checkpoint_db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                pending = pool.submit(request_delete)
                try:
                    assert waiting.wait(10), 'Deletion did not reach the running-job lock'
                    checkpoint_db.add(VisionCheckpoint(job_id=job_id, asset_id=images[0],
                        fingerprint='0' * 64, result={'persons': []}))
                    checkpoint_db.commit()
                finally:
                    checkpoint_db.rollback()
            response = pending.result(timeout=15)
        assert response.status_code == 409, response.text
        with app.state.db.Session() as db:
            asset = db.get(Asset, images[0])
            assert asset.status == 'clean'
            assert b''.join(app.state.storage.chunks(asset.object_key)) == original
            assert db.get(Job, job_id).status == 'running'
            assert db.scalar(select(VisionCheckpoint.id).where(VisionCheckpoint.job_id == job_id))
    finally:
        event.remove(app.state.db.engine, 'before_cursor_execute', before_query)
