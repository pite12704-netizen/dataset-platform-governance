import base64
"""Initialize a NEW private instance from a pinned offline bundle; never reuses business volumes."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid
import socket
import time
import shutil
import httpx

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT))


def command(args):
    blocked={'MYSQL_IMAGE','MINIO_IMAGE','MC_IMAGE','REDIS_IMAGE','CLAMAV_IMAGE','COMPOSE_FILE','COMPOSE_PROJECT_NAME','COMPOSE_PROFILES'}
    env={key:value for key,value in os.environ.items() if not key.startswith('DP_') and key not in blocked}
    result=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,env=env)
    if result.returncode:raise RuntimeError('Offline Docker operation failed: '+args[1])
    return result.stdout


def prepare(bundle,pinned,compiler=None,port_offset=0):
    from scripts.offline_delivery import verify
    from scripts.fresh_platform import env_text,compose_model
    from app.config import Settings
    manifest=verify(bundle,pinned)
    if not 0<=port_offset<=35000:raise RuntimeError('Invalid private port offset')
    if not pinned:raise RuntimeError('Trusted manifest hash is required')
    roles=manifest.get('image_roles',{})
    required={'api','web','scanner','mysql','minio','mc','redis'}
    if set(roles)!=required:raise RuntimeError('Offline installation requires every application and dependency image role')
    signatures=Path(bundle).resolve()/'signatures'
    if not any((signatures/name).is_file() for name in ('main.cvd','main.cld')) or not any((signatures/name).is_file() for name in ('daily.cvd','daily.cld')):
        raise RuntimeError('Offline virus signatures (main and daily) are required')
    active=ROOT/'var/run/active-deployment.json'
    if active.exists():raise RuntimeError('A managed instance already exists; offline initialization never resets it')
    project='dataset-platform-fresh-offline-'+uuid.uuid4().hex[:12]
    base=Path(os.environ['LOCALAPPDATA'])/'DatasetPlatform/instances' if os.name=='nt' else ROOT/'var/deployments'
    directory=base/project;secret_dir=directory/'secrets';secret_dir.mkdir(parents=True,exist_ok=False,mode=0o700)
    if os.name=='nt':
        user=os.environ['USERDOMAIN']+'\\'+os.environ['USERNAME']
        result=subprocess.run(['icacls',str(secret_dir),'/inheritance:r','/grant:r',user+':(OI)(CI)F'],capture_output=True)
        if result.returncode:raise RuntimeError('Cannot restrict offline secret permissions')
    values={name:secrets.token_hex(32) for name in ('app_key','mysql_password','mysql_root_password','minio_root_password','s3_secret_key','redis_password','backup_key')}
    values.update(minio_root_user='dp_storage_admin',s3_access_key='dp_'+secrets.token_hex(10),smtp_password='')
    values.update(session_signing_key=secrets.token_hex(32),mfa_encryption_keys=json.dumps([base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()]))
    values['database_url']='mysql+pymysql://dp_app:'+values['mysql_password']+'@mysql:3306/dataset_platform?charset=utf8mb4'
    values['redis_url']='redis://:'+values['redis_password']+'@redis:6379/0'
    values['database_host_url']=values['database_url'].replace('@mysql:3306/',f'@127.0.0.1:{13306+port_offset}/')
    values['redis_host_url']=values['redis_url'].replace('@redis:6379/',f'@127.0.0.1:{16379+port_offset}/')
    for name,value in values.items():(secret_dir/name).write_text(value,encoding='utf-8')
    staged_signatures=directory/'signatures';staged_signatures.mkdir()
    for name in ('main.cvd','main.cld','daily.cvd','daily.cld','bytecode.cvd','bytecode.cld'):
        if (signatures/name).is_file():shutil.copyfile(signatures/name,staged_signatures/name)
    defaults=Settings(_env_file=None,_env_prefix='OFFLINE_UNUSED_',secret_key='offline-settings-only-placeholder-0123456789').model_dump()
    for name in list(defaults):
        if name.endswith('_file') or name in {'secret_key','database_url','redis_url','s3_access_key','s3_secret_key','smtp_password','session_signing_key','mfa_encryption_keys'}:defaults.pop(name)
    defaults.update(environment='production',deployment='private',registration_mode='closed',require_verification=False,
        cookie_secure=True,storage_backend='s3',s3_endpoint='http://minio:9000',s3_bucket='datasets',task_mode='celery',
        scan_mode='clamav',clamav_host='clamav',public_url=f'https://localhost:{8443+port_offset}',frontend_url=f'https://localhost:{8443+port_offset}',
        cors_origins=[f'https://localhost:{8443+port_offset}'],model_url='',vision_model_url='',model_name='',vision_enabled=False,agent_tools_enabled=False)
    for name in ('secret_key','database_url','redis_url','s3_access_key','s3_secret_key','smtp_password','session_signing_key','mfa_encryption_keys'):
        defaults[name+'_file']='/run/secrets/'+('app_key' if name=='secret_key' else name)
    service=directory/'service.env.local';service.write_text(env_text(defaults),encoding='utf-8')
    host=dict(defaults)
    for name in ('secret_key','database_url','redis_url','s3_access_key','s3_secret_key','smtp_password','session_signing_key','mfa_encryption_keys'):
        file={'secret_key':'app_key','database_url':'database_host_url','redis_url':'redis_host_url'}.get(name,name)
        host[name+'_file']=str(secret_dir/file)
    host.update(s3_endpoint=f'http://127.0.0.1:{29000+port_offset}',clamav_host='127.0.0.1',clamav_port=13310+port_offset,mail_spool=str(directory/'mail'))
    host_file=directory/'host.env.local';host_file.write_text(env_text(host),encoding='utf-8')
    env={'DP_APP_IMAGE':roles['api'],'DP_WEB_IMAGE':roles['web'],'DP_SCAN_IMAGE':roles['scanner'],
        'MYSQL_IMAGE':roles['mysql'],'MINIO_IMAGE':roles['minio'],'MC_IMAGE':roles['mc'],'REDIS_IMAGE':roles['redis'],
        'CLAMAV_IMAGE':roles['scanner'],'DP_SERVICE_ENV':str(service),'DP_WEB_BIND':'127.0.0.1','DP_HTTPS_PORT':str(8443+port_offset)}
    compose_env=directory/'compose.env.local';compose_env.write_text('\n'.join(f'{name}={value}' for name,value in env.items())+'\n',encoding='utf-8')
    args=['docker','compose','--env-file',str(compose_env),'-f',str(ROOT/'deployment/compose.yml'),'-f',str(ROOT/'deployment/compose.local.yml'),'config','--format','json']
    model=(compiler or command)(args)
    if isinstance(model,str):model=json.loads(model)
    for name in ('storage-init.sh','bucket-policy.json'):
        shutil.copyfile(ROOT/'deployment'/name,directory/name)
    model=compose_model(model,project,directory,service,{})
    for name,definition in model['services'].items():
        for port in definition.get('ports',[]):
            if port.get('published') and name!='web':port['published']=str(int(port['published'])+port_offset)
    # Offline means dependencies are supplied locally. A normal private bridge
    # preserves host port publishing and access to approved intranet services.
    model['networks']['default']['internal']=False
    model['services']['clamav'].setdefault('environment',{})['CLAMAV_NO_FRESHCLAMD']='true'
    for definition in model['services'].values():definition['pull_policy']='never';definition.pop('build',None)
    compose_file=directory/'compose.json';compose_file.write_text(json.dumps(model,indent=2),encoding='utf-8')
    profile={'kind':'fresh-private','project':project,'directory':str(directory),'secrets_dir':str(secret_dir),
        'compose_env':str(compose_env),'compose_file':str(compose_file),'host_env':str(host_file),'service_env':str(service),
        'site_url':f'https://localhost:{8443+port_offset}','auto_model':False,'offline_bundle_manifest':pinned,'pending_initialization':True}
    (directory/'profile.json').write_text(json.dumps(profile,indent=2),encoding='utf-8')
    return profile,roles,staged_signatures


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bundle',required=True);parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--start',action='store_true');parser.add_argument('--port-offset',type=int,default=0);args=parser.parse_args()
    if args.start:
        for port in (number+args.port_offset for number in (8443,13306,29000,29001,16379,13310)):
            with socket.socket() as test:test.bind(('127.0.0.1',port))
    profile,roles,signatures=prepare(args.bundle,args.manifest_sha256,port_offset=args.port_offset)
    if args.start:
        copy="from pathlib import Path; import shutil; names={'main.cvd','main.cld','daily.cvd','daily.cld','bytecode.cvd','bytecode.cld'}; [shutil.copyfile(p,Path('/target')/p.name) for p in Path('/source').iterdir() if p.name in names]"
        command(['docker','run','--rm','--network','none','--user','0:0','--entrypoint','python','--mount',f'type=volume,source={profile["project"]}_clamav,target=/target',
            '--mount',f'type=bind,source={signatures},target=/source,readonly',roles['api'],'-c',copy])
        command(['docker','compose','-p',profile['project'],'--env-file',profile['compose_env'],'-f',profile['compose_file'],'up','-d','--no-build','--pull','never'])
        prefix=['docker','compose','-p',profile['project'],'--env-file',profile['compose_env'],'-f',profile['compose_file']]
        deadline=time.monotonic()+180
        while True:
            try:
                command([*prefix,'exec','-T','api','python','-c',"import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/health/ready',timeout=5).read()"])
                break
            except RuntimeError:
                if time.monotonic()>=deadline:raise RuntimeError('New offline instance did not become ready; existing instances were not modified')
                time.sleep(2)
        web=command([*prefix,'ps','-q','web']).strip()
        if len(web)<12 or any(char not in '0123456789abcdef' for char in web):raise RuntimeError('Invalid new web container identity')
        ca=Path(profile['directory'])/'public-root.crt';deadline=time.monotonic()+90
        while True:
            try:
                command(['docker','cp',web+':/data/caddy/pki/authorities/local/root.crt',str(ca)])
                response=httpx.get(profile['site_url']+'/api/v1/config',verify=str(ca),trust_env=False,timeout=5)
                if response.status_code==200 and response.json().get('deployment')=='private':break
            except (RuntimeError,httpx.HTTPError,ValueError):pass
            if time.monotonic()>deadline:raise RuntimeError('New HTTPS gateway is not ready; certificate verification was not bypassed')
            time.sleep(1)
        profile['pending_initialization']=False
        (Path(profile['directory'])/'profile.json').write_text(json.dumps(profile,indent=2),encoding='utf-8')
        active=ROOT/'var/run/active-deployment.json';active.parent.mkdir(parents=True,exist_ok=True)
        active.write_text(json.dumps(profile,indent=2),encoding='utf-8')
    print(json.dumps({'project':profile['project'],'prepared':True,'started':args.start,'private_registration':'closed','model_required':False}))


if __name__=='__main__':
    try:main()
    except (RuntimeError,OSError,ValueError):print('Offline initialization failed. Original instances were not changed; inspect the new private directory.',file=sys.stderr);raise SystemExit(1)
