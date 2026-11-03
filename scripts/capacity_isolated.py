"""Self-contained HTTP capacity harness; never accepts an existing service URL."""
import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
import uuid
import httpx

ROOT=Path(__file__).resolve().parents[1]


def serve(directory,port,namespace,datasets):
    import tracemalloc
    import uvicorn
    sys.path.insert(0,str(ROOT/'backend'))
    from app.config import Settings
    from app.main import create_app
    from app.auth import add_user
    from app.schemas import Register
    from app.models import Dataset,Version,Space
    from sqlalchemy import select
    root=Path(directory).resolve()
    if not root.is_relative_to(ROOT/'var/test-runs') or not root.name.startswith('capacity-'):raise RuntimeError('Not an isolated workspace')
    cfg=Settings(_env_file=None,environment='test',deployment='public',database_url='sqlite:///'+str(root/'test.db'),
        storage_backend='local',storage_root=root/'objects',task_mode='inline',scan_mode='development',
        secret_key='isolated-capacity-only-'+uuid.uuid4().hex,model_url='',vision_model_url='',smtp_host='',
        require_verification=False,require_admin_mfa=False,rate_limit_per_minute=100000,acceptance_namespace=namespace)
    app=create_app(cfg,initialize=True)
    with app.state.db.Session() as db:
        user=add_user(db,Register(username='synthetic-owner',name='Synthetic capacity owner',password='Synthetic-capacity-test-only!'),cfg,verified=True)
        space=db.scalar(select(Space).where(Space.owner_id==user.id))
        for index in range(datasets):
            dataset=Dataset(space_id=space.id,owner_id=user.id,name=f'Synthetic catalog {index}',visibility='public',access_mode='open')
            db.add(dataset);db.flush()
            db.add(Version(dataset_id=dataset.id,number=1,creator_id=user.id,status='published',license='synthetic-test-only',source='metadata-fixture-only'))
        db.commit()
    tracemalloc.start()
    @app.get('/internal/acceptance-stats')
    def stats():
        current,peak=tracemalloc.get_traced_memory();cpu=os.times()
        return {'namespace':namespace,'python_heap_current_bytes':current,'python_heap_peak_bytes':peak,'process_cpu_seconds':cpu.user+cpu.system}
    uvicorn.run(app,host='127.0.0.1',port=port,log_level='error',access_log=False)


async def exercise(url,namespace,duration,concurrency,maximum):
    async with httpx.AsyncClient(timeout=10,trust_env=False,limits=httpx.Limits(max_connections=concurrency,max_keepalive_connections=concurrency)) as client:
        config=(await client.get(url+'/api/v1/config')).json()
        if config.get('environment')!='test' or config.get('acceptance_namespace')!=namespace:raise RuntimeError('Isolation handshake failed')
        started=time.monotonic();deadline=started+duration;latencies=[];statuses=Counter();issued=0
        async def worker():
            nonlocal issued
            while time.monotonic()<deadline and issued<maximum:
                issued+=1;index=issued;begin=time.monotonic()
                try:
                    result=await client.get(url+('/api/v1/datasets?limit=12' if index%4 else '/health/live'))
                    statuses[str(result.status_code)]+=1
                except httpx.HTTPError:statuses['transport_error']+=1
                latencies.append((time.monotonic()-begin)*1000)
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        elapsed=time.monotonic()-started;ordered=sorted(latencies)
        resources=(await client.get(url+'/internal/acceptance-stats')).json()
    return {'requests':len(ordered),'elapsed_seconds':elapsed,'requests_per_second':len(ordered)/elapsed,
        'status_counts':dict(statuses),'latency_ms':{'p50':ordered[int((len(ordered)-1)*.5)],'p95':ordered[int((len(ordered)-1)*.95)],'max':ordered[-1]},
        'resources':resources}


def run(duration=30,concurrency=8,maximum=10000,datasets=200):
    directory=ROOT/'var/test-runs'/('capacity-'+uuid.uuid4().hex);directory.mkdir(parents=True)
    with socket.socket() as binding:binding.bind(('127.0.0.1',0));port=binding.getsockname()[1]
    namespace=uuid.uuid4().hex;env={key:value for key,value in os.environ.items() if not key.startswith('DP_')}
    log=(directory/'server.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve',str(directory),'--port',str(port),'--namespace',namespace,'--datasets',str(datasets)],
        cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        url=f'http://127.0.0.1:{port}';deadline=time.monotonic()+30
        while True:
            try:
                result=httpx.get(url+'/api/v1/config',timeout=1,trust_env=False).json()
                if result.get('acceptance_namespace')!=namespace:raise RuntimeError('Isolation mismatch')
                break
            except httpx.HTTPError:
                if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Isolated API did not start')
                time.sleep(.1)
        report=asyncio.run(exercise(url,namespace,duration,concurrency,maximum))
        report.update({'mode':'isolated-http-catalog','database':'temporary-sqlite','storage':'temporary-local',
            'api_workers':1,'concurrency':concurrency,'catalog_datasets':datasets,'python':platform.python_version(),'os':platform.system(),
            'tracemalloc_enabled':True,'business_services_accessed':False,
            'limitations':['Metadata HTTP workload only; no MySQL/MinIO, large-file, model or ClamAV capacity claim','Python heap excludes native allocations and operating-system RSS']})
        target=directory/'report.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return target,report
    finally:
        process.terminate()
        try:process.wait(5)
        except subprocess.TimeoutExpired:process.kill();process.wait(5)
        log.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--duration',type=int,default=30);parser.add_argument('--concurrency',type=int,default=8)
    parser.add_argument('--max-requests',type=int,default=10000);parser.add_argument('--datasets',type=int,default=200)
    parser.add_argument('--serve');parser.add_argument('--port',type=int);parser.add_argument('--namespace');args=parser.parse_args()
    if not 1<=args.duration<=3600 or not 1<=args.concurrency<=32 or not 1<=args.max_requests<=1000000 or not 1<=args.datasets<=10000:parser.error('Workload exceeds harness bounds')
    if args.serve:serve(args.serve,args.port,args.namespace,args.datasets);return 0
    target,report=run(args.duration,args.concurrency,args.max_requests,args.datasets)
    print(json.dumps({'report':str(target),'requests':report['requests'],'status_counts':report['status_counts']},ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
