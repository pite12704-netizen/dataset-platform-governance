"""通过 HTTPS 入口验证真实异步上传与质量任务；需要明确传入本地验收账号文件。

只创建标明运维验收的私人草稿，不发布数据，不批准数据或人工复核。
"""
import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.config import Settings
from app.db import Database
from app.models import Asset,Job
from sqlalchemy.engine import make_url

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accounts',required=True)
    parser.add_argument('--env-file',default='.env.business.local')
    parser.add_argument('--url',default='https://localhost:8443')
    parser.add_argument('--ca',default='var/operations/local-ca.crt')
    args=parser.parse_args()
    settings=Settings(_env_file=args.env_file)
    if make_url(settings.database_url).database!='dataset_platform' or settings.task_mode!='celery':
        parser.error('Real business store and Celery profile required')
    owner=json.loads(Path(args.accounts).read_text(encoding='utf-8'))['owner']
    database=Database(settings.database_url,settings)
    def wait(check):
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            with database.Session() as db:
                result=check(db)
                if result:
                    return result
            time.sleep(1)
        raise RuntimeError('Independent Worker did not finish within 120 seconds')
    try:
        with httpx.Client(base_url=args.url,verify=args.ca,trust_env=False,timeout=30) as client:
            def call(method,path,**kwargs):
                r=client.request(method,path,**kwargs)
                if r.is_error:
                    raise RuntimeError(f'{method} {path}: HTTP {r.status_code}')
                return r.json()
            assert call('GET','/health/ready')['status']=='ready'
            result=call('POST','/api/v1/auth/login',json={k:owner[k] for k in ('email','password')})
            client.headers['Authorization']='Bearer '+result['access_token']
            space=call('GET','/api/v1/spaces')[0]['id']
            ds=call('POST','/api/v1/datasets',json={'space_id':space,'name':'运维验收：真实异步任务（私人草稿）','visibility':'private','access_mode':'application'})
            version=call('POST',f"/api/v1/datasets/{ds['id']}/versions",json={'license':'internal-operations-fixture','source':'Synthetic operational acceptance fixture; not training data'})
            raw=b'x,y\n'+b'a,b\n'*(2300*1024)
            upload=call('POST',f"/api/v1/versions/{version['id']}/uploads",json={'filename':'operations.csv','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'idempotency_key':uuid.uuid4().hex})
            for index,offset in enumerate(range(0,len(raw),upload['part_size']),1):
                call('PUT',f"/api/v1/uploads/{upload['id']}/parts/{index}",content=raw[offset:offset+upload['part_size']])
            asset_id=call('POST',f"/api/v1/uploads/{upload['id']}/complete")['asset_id']
            wait(lambda db: db.get(Asset,asset_id).status=='clean')
            job=call('POST','/api/v1/jobs',json={'version_id':version['id'],'kind':'quality','idempotency_key':uuid.uuid4().hex})
            wait(lambda db: db.get(Job,job['id']).status=='completed')
            with database.Session() as db:
                scan=db.get(Asset,asset_id)
                quality=db.get(Job,job['id'])
                assert scan.checks['malware_method']=='clamav' and quality.result['passed']==1
            output={'verified':True,'https_verified':True,'storage':'MySQL+MinIO','task_mode':'celery','dataset_id':ds['id'],'asset_id':asset_id,'job_id':job['id'],'bytes':len(raw),'parts':index,'scan':'clamav','quality':'completed'}
            (ROOT/'var/operations/runtime-validation.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
            print(json.dumps(output))
    finally:
        database.engine.dispose()

if __name__=='__main__':
    main()
