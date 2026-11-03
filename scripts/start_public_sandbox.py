"""Fresh localhost learning sandbox; never reads an existing deployment or database."""
import argparse,json,os,secrets,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'));sys.path.insert(0,str(ROOT))

def prepare(root=ROOT,port=18080):
    if not 1024<=port<=65535:raise ValueError('Port must be 1024..65535')
    directory=Path(root)/'var/sandboxes'/('sandbox-'+uuid.uuid4().hex);directory.mkdir(parents=True,exist_ok=False)
    from app.config import Settings
    cfg=Settings(_env_file=None,_env_prefix='SANDBOX_UNUSED_',environment='development',deployment='private',
        secret_key=secrets.token_hex(32),database_url='sqlite:///'+str(directory/'metadata.db'),
        storage_backend='local',storage_root=directory/'objects',task_mode='inline',scan_mode='development',
        model_url='',model_name='',vision_model_url='',vision_model_name='',vision_enabled=False,agent_tools_enabled=False,
        smtp_host='',oidc_issuer='',registration_mode='open',require_verification=False,require_admin_mfa=False,
        public_url=f'http://127.0.0.1:{port}',frontend_url=f'http://127.0.0.1:{port}',cookie_secure=False,
        mail_spool=directory/'mail',redis_url='redis://127.0.0.1:1/0')
    env=directory/'settings.env'
    lines=[]
    for k,v in cfg.model_dump(mode='json').items():
        if v is not None:lines.append('DP_'+k.upper()+'='+json.dumps(v,ensure_ascii=False))
    env.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    try:env.chmod(0o600)
    except OSError:pass
    # Only this child process is changed. No active-profile or host configuration is read.
    for key in list(os.environ):
        if key.startswith('DP_'):os.environ.pop(key)
    os.environ['DP_SETTINGS_FILE']=str(env)
    from app.config import get_settings
    get_settings.cache_clear()
    from alembic.config import Config
    from alembic import command
    migration=Config(str(ROOT/'backend/alembic.ini'));migration.attributes['settings']=cfg
    command.upgrade(migration,'head')
    (directory/'sandbox.json').write_text(json.dumps({'isolated':True,'production':False,'models_enabled':False,'port':port}),encoding='utf-8')
    return cfg,directory

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=18080);p.add_argument('--prepare-only',action='store_true');a=p.parse_args()
    cfg,directory=prepare(port=a.port)
    print(json.dumps({'isolated':True,'production':False,'directory':str(directory),'url':cfg.public_url,'models_enabled':False}),flush=True)
    if a.prepare_only:return
    dist=ROOT/'frontend/dist'
    if not (dist/'index.html').is_file():raise RuntimeError('Build frontend first: npm --prefix frontend run build')
    from app.main import create_app
    from fastapi import HTTPException
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    app=create_app(cfg);app.mount('/assets',StaticFiles(directory=dist/'assets'),name='sandbox-assets')
    @app.get('/{page:path}',include_in_schema=False)
    def page(page:str):
        if page.split('/')[0] in {'api','health','mcp','.well-known'}:raise HTTPException(404)
        return FileResponse(dist/'index.html')
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=a.port,log_level='warning')
if __name__=='__main__':main()
