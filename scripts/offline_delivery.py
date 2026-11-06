"""Build, verify and load a credential-free offline release. Never starts business services."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.build_fingerprint import fingerprint
ALLOWED_ROOTS={'backend','clients','scripts','deployment','docs','config','frontend'}
EXCLUDED={'var','.git','.venv','.venv-cloud','node_modules','dist','secrets','__pycache__','usermanage'}
EXTENSIONS={'.py','.ps1','.sh','.yml','.yaml','.ini','.mako','.toml','.txt','.lock','.md','.json','.ts','.tsx','.css','.html','.svg'}


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def source_files():
    for directory,dirs,files in os.walk(ROOT):
        dirs[:]=[name for name in dirs if name not in EXCLUDED and not (Path(directory)/name).is_symlink()]
        for name in files:
            path=Path(directory)/name;relative=path.relative_to(ROOT)
            if path.is_symlink():continue
            if relative.parts[0] not in ALLOWED_ROOTS and str(relative) not in {'README.md','AGENTS.md','CODE_STUDY_GUIDE.md','start-platform.ps1','pyproject.toml','converter_core.py','.gitignore','.dockerignore'}:continue
            if '.local' in name and relative.as_posix()!='deployment/compose.local.yml' or '.env' in name and not name.endswith('.example') or name.startswith('.') and name not in {'.gitignore','.dockerignore'} or 'clipboard' in name:continue
            if path.suffix.lower() in EXTENSIONS or name.startswith(('Caddyfile','Dockerfile')) or name.endswith('.example') or name in {'.gitignore','.dockerignore'}:yield path,relative


def verify_api_source(image,root,runner=subprocess.run):
    try:
        result=runner(['docker','run','--rm','--pull','never','--network','none','--read-only','--entrypoint','cat',
            image,'/app/build-info.json'],capture_output=True,text=True,timeout=30)
        observed=json.loads(result.stdout) if not result.returncode else {}
        expected=fingerprint(root)
        if observed!=expected:raise RuntimeError('API image/source fingerprint mismatch; rebuild the candidate image')
        return expected['fingerprint']
    except (ValueError,subprocess.TimeoutExpired):
        raise RuntimeError('Cannot verify API image/source fingerprint; rebuild the candidate image') from None


def checked_file(bundle,name):
    relative=Path(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name:raise RuntimeError('Invalid bundle path')
    path=bundle/relative
    if any(part.is_symlink() for part in [path,*path.parents] if part!=bundle.parent):raise RuntimeError('Symlink not allowed')
    if not path.resolve().is_relative_to(bundle.resolve()):raise RuntimeError('Bundle path escaped root')
    return path


def verify(bundle,pinned=None):
    bundle=Path(bundle).resolve();manifest_path=bundle/'manifest.json'
    if manifest_path.is_symlink():raise RuntimeError('Manifest links are forbidden')
    if manifest_path.stat().st_size>16*1024**2:raise RuntimeError('Offline manifest exceeds limit')
    if pinned and sha(manifest_path)!=pinned:raise RuntimeError('Manifest authentication hash mismatch')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format')!=1 or not isinstance(manifest.get('files'),list):raise RuntimeError('Invalid offline manifest')
    seen=set()
    for entry in manifest['files']:
        if entry['path'] in seen:raise RuntimeError('Duplicate bundle file')
        seen.add(entry['path']);path=checked_file(bundle,entry['path'])
        if not path.is_file() or path.stat().st_size!=entry['size'] or sha(path)!=entry['sha256']:raise RuntimeError('Offline file verification failed')
    for item in manifest.get('images',[]):
        if item['path'] not in seen or not re.fullmatch(r'sha256:[a-f0-9]{64}',item['id']):raise RuntimeError('Invalid image descriptor')
    if not set(manifest.get('image_roles',{}).values())<={item['id'] for item in manifest.get('images',[])}:
        raise RuntimeError('Image roles reference files outside the bundle')
    actual={path.relative_to(bundle).as_posix() for path in bundle.rglob('*') if path.is_file()}
    if actual!=seen|{'manifest.json'}:raise RuntimeError('Unlisted bundle file')
    return manifest


def build(output,images,wheelhouse=None,signatures=None,reuse=None,reuse_hash=None):
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    schema_revision = ScriptDirectory.from_config(Config(str(ROOT/'backend/alembic.ini'))).get_current_head()
    # The repository audit does not stage anything and never includes local service files.
    if subprocess.run([sys.executable,str(ROOT/'scripts/audit_repository.py')],cwd=ROOT,capture_output=True).returncode:
        raise RuntimeError('Repository audit rejected offline source packaging')
    cache=verify(reuse,reuse_hash) if reuse and reuse_hash else None
    descriptors=[];roles={}
    for ref in images:
        if '=' in ref:
            role,ref=ref.split('=',1)
            if role not in {'api','web','scanner','mysql','minio','mc','redis'} or role in roles:raise RuntimeError('Invalid image role')
        else:role=None
        if not ref or ref.startswith('-'):raise RuntimeError('Invalid image reference')
        result=subprocess.run(['docker','image','inspect',ref,'--format','{{.Id}}'],capture_output=True,text=True)
        identifier=result.stdout.strip()
        if result.returncode or not re.fullmatch(r'sha256:[a-f0-9]{64}',identifier):raise RuntimeError('Required local image unavailable; build the release images first')
        descriptors.append((ref,identifier))
        if role:roles[role]=identifier
    if 'api' in roles:
        probe="from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config('/app/backend/alembic.ini')).get_current_head())"
        try:result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp','--entrypoint','python',roles['api'],'-c',probe],capture_output=True,text=True,timeout=30)
        except subprocess.TimeoutExpired:raise RuntimeError('Application image revision probe timed out') from None
        if result.returncode or result.stdout.strip()!=schema_revision:raise RuntimeError('Application image schema differs from offline source release')
        api_fingerprint=verify_api_source(roles['api'],ROOT)
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    manifest={'format':1,'schema_revision':schema_revision,'host_python':f'{sys.version_info.major}.{sys.version_info.minor}',
        'host_platform':sys.platform,'files':[],'images':[],'image_roles':roles,'local_service_credentials_included':False,
        'prerequisites':['Docker Linux engine and Compose','Python matching host_python','New local secrets and TLS configuration',
                         'ClamAV signatures and model weights provisioned separately; base data platform works without a model']}
    for index,(ref,identifier) in enumerate(descriptors):
        name=f'images/{index:02d}.tar';path=output/name;path.parent.mkdir(exist_ok=True)
        cached=next((item for item in cache.get('images',[]) if item['id']==identifier),None) if cache else None
        if cached:
            source=checked_file(Path(reuse).resolve(),cached['path'])
            try:os.link(source,path)
            except OSError:shutil.copyfile(source,path)
        else:
            try:result=subprocess.run(['docker','save','--output',str(path),ref],capture_output=True,timeout=1800)
            except subprocess.TimeoutExpired:raise RuntimeError('Offline image export timed out; incomplete directory preserved') from None
            if result.returncode:raise RuntimeError('Offline image export failed; incomplete directory was preserved')
        manifest['images'].append({'reference':ref,'id':identifier,'path':name})
    for path,relative in source_files():
        target=output/'source'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    if 'api' in roles:
        if fingerprint(output/'source')['fingerprint']!=api_fingerprint:raise RuntimeError('Source changed during packaging; candidate bundle is not releasable')
        manifest['api_source_fingerprint']=api_fingerprint
    if wheelhouse:
        destination=output/'wheelhouse';destination.mkdir()
        for path in Path(wheelhouse).glob('*.whl'):
            if path.is_symlink():raise RuntimeError('Wheelhouse links are forbidden')
            shutil.copyfile(path,destination/path.name)
    if signatures:
        destination=output/'signatures';destination.mkdir()
        allowed={'main.cvd','daily.cvd','bytecode.cvd','main.cld','daily.cld','bytecode.cld'}
        for name in allowed:
            path=Path(signatures)/name
            if path.is_symlink():raise RuntimeError('Signature links are forbidden')
            if path.is_file():shutil.copyfile(path,destination/name)
    for path in sorted(output.rglob('*')):
        if path.is_file():manifest['files'].append({'path':path.relative_to(output).as_posix(),'size':path.stat().st_size,'sha256':sha(path)})
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    verify(output)
    return {'manifest_sha256':sha(output/'manifest.json'),'files':len(manifest['files']),'images':len(descriptors)}


def load(bundle,pinned):
    if not pinned:raise RuntimeError('Loading requires an independently obtained manifest SHA256')
    manifest=verify(bundle,pinned)
    for item in manifest['images']:
        path=checked_file(Path(bundle).resolve(),item['path'])
        if subprocess.run(['docker','load','--input',str(path)],capture_output=True).returncode:raise RuntimeError('Docker load failed')
        result=subprocess.run(['docker','image','inspect',item['id'],'--format','{{.Id}}'],capture_output=True,text=True)
        if result.returncode or result.stdout.strip()!=item['id']:raise RuntimeError('Loaded image identity mismatch')
    return {'loaded_images':len(manifest['images']),'services_started':False,'database_modified':False}


def install_tools(bundle,pinned,project):
    manifest=verify(bundle,pinned)
    if not pinned:raise RuntimeError('Installation requires a trusted manifest hash')
    if manifest['host_platform']!=sys.platform or manifest['host_python']!=f'{sys.version_info.major}.{sys.version_info.minor}':
        raise RuntimeError('Host Python/platform differs from wheelhouse target')
    if not list((Path(bundle)/'wheelhouse').glob('*.whl')):raise RuntimeError('No offline wheelhouse was included')
    target=Path(project).resolve()
    if target.is_relative_to(Path(bundle).resolve()):raise RuntimeError('Install outside the immutable bundle')
    target.mkdir(parents=True,exist_ok=False)
    for entry in manifest['files']:
        if not entry['path'].startswith('source/'):continue
        source=checked_file(Path(bundle).resolve(),entry['path']);destination=target/Path(entry['path']).relative_to('source')
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,destination)
    if subprocess.run([sys.executable,'-m','venv',str(target/'.venv')],capture_output=True).returncode:raise RuntimeError('Offline host runtime creation failed')
    python=target/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if subprocess.run([str(python),'-m','pip','install','--no-index','--find-links',str(Path(bundle).resolve()/'wheelhouse'),
        '-r',str(target/'backend/requirements.lock')],capture_output=True).returncode:raise RuntimeError('Offline dependency installation failed')
    return {'project':str(target),'services_started':False,'database_modified':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['build','verify','load','wheels','install-tools'])
    parser.add_argument('--bundle',required=True);parser.add_argument('--image',action='append',default=[]);parser.add_argument('--wheelhouse')
    parser.add_argument('--manifest-sha256');parser.add_argument('--clamav-database');parser.add_argument('--project-directory')
    parser.add_argument('--reuse-bundle');parser.add_argument('--reuse-manifest-sha256');args=parser.parse_args()
    if args.action=='wheels':
        target=Path(args.bundle);target.mkdir(parents=True,exist_ok=False)
        return subprocess.run([sys.executable,'-m','pip','download','--only-binary=:all:','--requirement',str(ROOT/'backend/requirements.lock'),'--dest',str(target)]).returncode
    if args.action=='build':
        if not args.image:parser.error('Supply every exact image needed by the chosen release Compose configuration')
        result=build(args.bundle,args.image,args.wheelhouse,args.clamav_database,args.reuse_bundle,args.reuse_manifest_sha256)
    elif args.action=='install-tools':
        if not args.project_directory:parser.error('Supply a new project directory')
        result=install_tools(args.bundle,args.manifest_sha256,args.project_directory)
    elif args.action=='load':result=load(args.bundle,args.manifest_sha256)
    else:
        manifest=verify(args.bundle,args.manifest_sha256);result={'verified':True,'manifest_authenticated':bool(args.manifest_sha256),'files':len(manifest['files'])}
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,OSError,ValueError):print('Offline operation failed; inspect prerequisites and manifest. No business service was started.',file=sys.stderr);raise SystemExit(1)
