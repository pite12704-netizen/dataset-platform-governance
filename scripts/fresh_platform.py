import base64
"""创建全新私有本机实例，保留旧数据卷；不读取或搬运旧账号、图片和业务记录。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ROOT/'var/run/active-deployment.json'
sys.path.insert(0, str(ROOT/'backend'))


def run(*args, check=True):
    reserved = {'DP_APP_IMAGE','DP_WEB_IMAGE','DP_SCAN_IMAGE','DP_SERVICE_ENV','DP_WEB_BIND','DP_HTTPS_PORT','DP_SITE_ADDRESS',
                'MYSQL_IMAGE','MINIO_IMAGE','MC_IMAGE','REDIS_IMAGE','CLAMAV_IMAGE'}
    env = {key:value for key,value in os.environ.items() if key not in reserved}
    result = subprocess.run(list(args), cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8')
    if check and result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(args[:2]))
    return result.stdout.strip()


def save(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    try:
        temporary.write_text(content, encoding='utf-8'); os.chmod(temporary, 0o600); temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def env_text(values):
    def scalar(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return str(value)
    return '\n'.join('DP_'+key.upper()+'='+scalar(value) for key,value in values.items() if value is not None)+'\n'


def volume_at(container, target):
    mounts = json.loads(run('docker','inspect','--format','{{json .Mounts}}',container))
    return next(item['Name'] for item in mounts if item['Type']=='volume' and item['Destination']==target)


def compose_model(model, project, directory, service_env, shared):
    """所有业务卷、网络和密钥重建；仅沿用本机 TLS 和公开恶意文件特征库。"""
    model['name'] = project
    model['networks'] = {'default': {'name': project+'_default'}}
    model['volumes'] = {name: ({'external':True,'name':shared[name]} if name in shared else {'name':project+'_'+name})
                        for name in model['volumes']}
    model['secrets'] = {name:{'file':str(directory/'secrets'/name)} for name in model['secrets']}
    model['secrets']['smtp_password'] = {'file':str(directory/'secrets/smtp_password')}
    for name in ('api','worker','vision-worker','mail-worker','beat','migrate'):
        if name=='mail-worker' and name not in model['services']:continue
        service = model['services'][name]
        service['env_file'] = [str(service_env)]
        service['environment'] = {'DP_WORKER_ROLE':{'vision-worker':'vision','mail-worker':'mail'}[name]} if name in {'vision-worker','mail-worker'} else {}
        service['image'] = '${DP_APP_IMAGE}'
        service['secrets'].append({'source':'smtp_password','target':'smtp_password'})
    model['services']['web']['image'] = '${DP_WEB_IMAGE}'
    model['services']['clamav']['image'] = '${DP_SCAN_IMAGE}'
    for mount in model['services']['storage-init']['volumes']:
        mount['source'] = str(directory/Path(mount['target']).name)
        mount['bind'] = {'create_host_path':False}
    ports = {'mysql': [('13306','3306')], 'minio':[('29000','9000'),('29001','9001')],
             'redis':[('16379','6379')], 'clamav':[('13310','3310')]}
    for name, entries in ports.items():
        model['services'][name]['ports'] = [{'host_ip':'127.0.0.1','published':host,'target':int(target),'protocol':'tcp'}
                                           for host,target in entries]
    return model


def create():
    if ACTIVE.exists():
        raise RuntimeError('A fresh deployment is already active; use enterprise.ps1 to start it')
    suffix = time.strftime('%Y%m%d')+'-'+uuid.uuid4().hex[:6]
    project = 'dataset-platform-fresh-'+suffix
    base = Path(os.environ['LOCALAPPDATA'])/'DatasetPlatform/instances' if os.name=='nt' else ROOT/'var/deployments'
    directory = base/project; directory.mkdir(parents=True, exist_ok=False)
    secret_dir = directory/'secrets'; secret_dir.mkdir()
    print('Preparing a new empty private deployment...', flush=True)
    values = {name:secrets.token_hex(32) for name in ('app_key','mysql_password','mysql_root_password','minio_root_password',
        's3_secret_key','redis_password','backup_key')}
    values.update(minio_root_user='dp_storage_admin',s3_access_key='dp_'+secrets.token_hex(10),smtp_password='')
    values.update(session_signing_key=secrets.token_hex(32),mfa_encryption_keys=json.dumps([base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()]))
    values['database_url'] = 'mysql+pymysql://dp_app:'+values['mysql_password']+'@mysql:3306/dataset_platform?charset=utf8mb4'
    values['redis_url'] = 'redis://:'+values['redis_password']+'@redis:6379/0'
    for name,value in values.items():
        save(secret_dir/name,value)
    save(secret_dir/'database_host_url',values['database_url'].replace('@mysql:3306/','@127.0.0.1:13306/'))
    save(secret_dir/'redis_host_url',values['redis_url'].replace('@redis:6379/','@127.0.0.1:16379/'))
    from app.config import Settings
    defaults = Settings(_env_file=None,_env_prefix='FRESH_SETUP_UNUSED_',secret_key='unused-setup-placeholder-01234567890123456789').model_dump()
    for key in ('secret_key','database_url','s3_access_key','s3_secret_key','redis_url','smtp_password','session_signing_key','mfa_encryption_keys'):
        defaults.pop(key,None)
    defaults.update(environment='production',deployment='private',registration_mode='open',require_verification=False,
        cookie_secure=True,storage_backend='s3',s3_endpoint='http://minio:9000',s3_bucket='datasets',
        task_mode='celery',scan_mode='clamav',clamav_host='clamav',public_url='https://localhost:8443',
        frontend_url='https://localhost:8443',cors_origins=['https://localhost:8443'],
        model_url='http://host.docker.internal:1234/v1',model_name='image-pilot-qwen3-vl-4b',
        model_resource_group='local-inference',agent_tools_enabled=False,vision_enabled=False)
    manifest = ROOT/'var/image-pilot/lmstudio/download_manifest.json'
    if manifest.exists():
        weight = json.loads(manifest.read_text()).get('files',{}).get('Qwen3-VL-4B-Instruct-Q4_K_M.gguf',{})
        if weight.get('sha256'):
            defaults.update(vision_enabled=True,vision_model_revision='Qwen3-VL-4B-Instruct-Q4_K_M:sha256:'+weight['sha256'])
    for key in ('secret_key','database_url','s3_access_key','s3_secret_key','redis_url','smtp_password','session_signing_key','mfa_encryption_keys'):
        filename = {'secret_key':'app_key'}.get(key,key)
        defaults[key+'_file'] = '/run/secrets/'+filename
    service_env = directory/'service.env.local'; save(service_env,env_text(defaults))
    host = dict(defaults)
    for key in ('secret_key','database_url','s3_access_key','s3_secret_key','redis_url','smtp_password','session_signing_key','mfa_encryption_keys'):
        filename = {'secret_key':'app_key','database_url':'database_host_url','redis_url':'redis_host_url'}.get(key,key)
        host[key+'_file'] = str(secret_dir/filename)
    host.update(s3_endpoint='http://127.0.0.1:29000',clamav_host='127.0.0.1',clamav_port=13310,
                model_url='http://127.0.0.1:1234/v1',mail_spool=directory/'mail')
    host_env = directory/'host.env.local'; save(host_env,env_text(host))
    images = {}
    for line in (ROOT/'deployment/compose.env.local').read_text().splitlines():
        name,_,value = line.partition('=')
        if name in {'MYSQL_IMAGE','MINIO_IMAGE','MC_IMAGE','REDIS_IMAGE','CLAMAV_IMAGE'}:
            images[name] = value
    images.update(DP_APP_IMAGE='dataset-platform-api:'+suffix,DP_WEB_IMAGE='dataset-platform-web:'+suffix,
                  DP_SCAN_IMAGE='dataset-platform-scanner:'+suffix,DP_SERVICE_ENV=str(service_env),
                  DP_WEB_BIND='127.0.0.1',DP_HTTPS_PORT='8443',DP_SITE_ADDRESS='localhost')
    compose_env = directory/'compose.env.local'
    save(compose_env,'\n'.join(k+'='+v for k,v in images.items())+'\n')
    model = json.loads(run('docker','compose','-p',project,'--env-file',str(compose_env),'-f','deployment/compose.yml','config','--format','json'))
    shared = {'caddy_data':volume_at('dataset-platform-web-1','/data'),
              'caddy_config':volume_at('dataset-platform-web-1','/config'),
              'clamav':volume_at('dataset-platform-clamav-1','/var/lib/clamav')}
    for name in ('storage-init.sh','bucket-policy.json'):
        shutil.copyfile(ROOT/'deployment'/name,directory/name)
    model = compose_model(model,project,directory,service_env,shared)
    compose_file = directory/'compose.json'; save(compose_file,json.dumps(model,ensure_ascii=False,indent=2))
    args = ['docker','compose','-p',project,'--env-file',str(compose_env),'-f',str(compose_file)]
    run(*args,'config','-q')
    # 所有构建发生在停止旧站点之前；失败不影响其运行。
    run(*args,'build','api','web','clamav')
    for key in ('DP_APP_IMAGE','DP_WEB_IMAGE','DP_SCAN_IMAGE'):
        images[key] = run('docker','image','inspect',images[key],'--format','{{.Id}}')
    save(compose_env,'\n'.join(k+'='+v for k,v in images.items())+'\n')
    old_ids = run('docker','ps','-q','--filter','label=com.docker.compose.project=dataset-platform').split()
    # 旧环境停用前确认没有活跃作业；只读查询，不执行旧库迁移。
    check = "from app.config import get_settings; from sqlalchemy import create_engine,text; e=create_engine(get_settings().database_url); c=e.connect(); print(c.scalar(text(\"SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')\"))+c.scalar(text(\"SELECT COUNT(*) FROM uploads WHERE status='uploading'\"))); c.close(); e.dispose()"
    if run('docker','exec','dataset-platform-api-1','python','-c',check) != '0':
        raise RuntimeError('Existing work is active; finish it before switching environments')
    run('docker','cp','dataset-platform-web-1:/data/caddy/pki/authorities/local/root.crt',str(directory/'root-ca.crt'))
    context = ssl.create_default_context(cafile=str(directory/'root-ca.crt'))
    try:
        if old_ids:
            run('docker','stop',*old_ids)
        print('Starting new MySQL, private storage and workers; creating tables in the empty database...',flush=True)
        run(*args,'up','-d','--no-build')
        with httpx.Client(verify=context,trust_env=False,timeout=5) as client:
            for attempt in range(90):
                try:
                    response = client.get('https://localhost:8443/health/ready')
                    if response.status_code==200:
                        config = client.get('https://localhost:8443/api/v1/config').json()
                        if config.get('registration_enabled') and not config.get('registration_email_required'):
                            break
                except httpx.HTTPError:
                    pass
                if attempt==89:
                    raise RuntimeError('New instance did not pass readiness and registration checks')
                time.sleep(2)
        profile = {'kind':'fresh-private','project':project,'compose_file':str(compose_file),'compose_env':str(compose_env),
            'service_env':str(service_env),'host_env':str(host_env),'secrets_dir':str(secret_dir),
            'site_url':'https://localhost:8443','directory':str(directory),'auto_model':defaults['vision_enabled'],
            'old_container_ids':old_ids,'old_project':'dataset-platform'}
        save(directory/'profile.json',json.dumps(profile,ensure_ascii=False,indent=2))
        save(ACTIVE,json.dumps(profile,ensure_ascii=False,indent=2))
        print('New empty private platform ready: https://localhost:8443/ ; register your own accounts.',flush=True)
        print('Old business volumes retained; startup now uses var/run/active-deployment.json.',flush=True)
    except Exception:
        run(*args,'stop',check=False)
        if old_ids:
            run('docker','start',*old_ids,check=False)
        raise


if __name__=='__main__':
    try:
        argparse.ArgumentParser(description=__doc__).parse_args()
        create()
    except Exception as exc:
        print('Fresh deployment failed: '+str(exc) if isinstance(exc,RuntimeError) else 'Fresh deployment failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
