"""Export a reviewed public-source snapshot without history, runtime data or private docs."""
import argparse,hashlib,json,os,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts import audit_repository as audit

ROOT_FILES={'.gitignore','.dockerignore','AGENTS.md','pyproject.toml','converter_core.py','start-platform.ps1',
    'LICENSE','THIRD_PARTY_NOTICES.md','SECURITY.md','CONTRIBUTING.md'}
TREES={'backend','frontend','clients','deployment','scripts','.github'}
EXTENSIONS={'.py','.mjs','.ts','.tsx','.css','.html','.json','.yml','.yaml','.toml','.ini','.mako','.md','.txt','.lock','.ps1','.sh','.svg','.example'}
OVERRIDES={'README.md':'docs/public/README.md','CODE_STUDY_GUIDE.md':'docs/public/CODE_STUDY_GUIDE.md',
    'docs/public/architecture.md':'docs/public/architecture.md','docs/public/deployment.md':'docs/public/deployment.md',
    'docs/public/github-publication.md':'docs/public/github-publication.md',
    'docs/cloud-development.md':'docs/public/cloud-development.md'}
PRIVATE=re.compile(r'(?i:[A-Za-z]:[\\/](?:Dataset Platform|Users[\\/][A-Za-z0-9_.-]+[\\/]|DatasetPlatform(?:Runtime|Recovery)))|/Users/[A-Za-z0-9_.-]+/')

def selected(name):
    p=Path(name)
    if audit.forbidden_path(name):return False
    if name in ROOT_FILES:return True
    if name=='config/development.json' or name.startswith('config/examples/'):return p.suffix=='.json'
    return p.parts[0] in TREES and (p.suffix in EXTENSIONS or p.name.startswith(('Dockerfile','Caddyfile')))

def files(root):
    for directory,dirs,names in os.walk(root):
        dirs[:]=[d for d in dirs if d not in audit.EXCLUDED and not (Path(directory)/d).is_symlink()]
        for name in names:
            path=Path(directory)/name
            if not path.is_symlink():yield path,path.relative_to(root).as_posix()

def inspect(root,verify_manifest=False):
    root=Path(root);findings=[];paths={}
    if (root/'.git').exists():
        p=subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True,check=True)
        for name in p.stdout.decode().strip('\0').split('\0'):
            if name:paths[name]=root/name
    else:paths={name:path for path,name in files(root)}
    secrets=audit.local_secrets()
    for name,path in paths.items():
        allowed=name in OVERRIDES or selected(name) or name=='public-source.json'
        if path.is_symlink() or not path.is_file():findings.append({'path':name,'reason':'not_a_regular_file'});continue
        if not allowed:findings.append({'path':name,'reason':'outside_public_scope'});continue
        data=path.read_bytes()
        if len(data)>10*1024**2 or b'\0' in data:findings.append({'path':name,'reason':'binary_or_large_file'});continue
        text=data.decode('utf-8','strict')
        findings+=audit.findings_for(name,text,secrets)
        if PRIVATE.search(text):findings.append({'path':name,'reason':'private_machine_path'})
    if not (root/'LICENSE').is_file():findings.append({'path':'LICENSE','reason':'missing_license'})
    if verify_manifest:
        manifest=json.loads((root/'public-source.json').read_text(encoding='utf-8'))
        for entry in manifest['files']:
            name=entry['path'];target=root/name
            if Path(name).is_absolute() or '..' in Path(name).parts or not target.resolve().is_relative_to(root.resolve()):raise RuntimeError('Invalid manifest path')
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=entry['sha256']:findings.append({'path':name,'reason':'snapshot_changed'})
        if set(paths)-{'public-source.json'}!={e['path'] for e in manifest['files']}:findings.append({'path':'public-source.json','reason':'snapshot_file_set_changed'})
    return {'files':len(paths),'findings':findings,'passed':not findings}

def export(destination):
    destination=Path(destination).resolve()
    if destination.exists():raise RuntimeError('Choose a new empty destination; existing directories are never overwritten')
    if destination==ROOT.resolve() or ROOT.resolve().is_relative_to(destination):raise RuntimeError('Destination must not contain the source workspace')
    if destination.is_relative_to(ROOT.resolve()) and destination.relative_to(ROOT.resolve()).parts[0]!='var':raise RuntimeError('Exports inside the workspace must stay under ignored var/')
    choices={name:path for path,name in files(ROOT) if selected(name)}
    for name,source in OVERRIDES.items():choices[name]=ROOT/source
    for required in ['backend/app/main.py','frontend/package-lock.json','LICENSE',*OVERRIDES]:
        if required not in choices or not choices[required].is_file():raise RuntimeError('Missing public-source prerequisite')
    # Audit before creating the destination; no original Git index or service is mutated.
    secrets=audit.local_secrets();findings=[]
    for name,path in choices.items():
        data=path.read_bytes();text=data.decode('utf-8','strict')
        findings+=audit.findings_for(name,text,secrets)
        if PRIVATE.search(text):findings.append({'path':name,'reason':'private_machine_path'})
        if b'\0' in data or len(data)>10*1024**2:findings.append({'path':name,'reason':'binary_or_large_file'})
    if findings:return {'passed':False,'findings':findings}
    destination.mkdir(parents=True,exist_ok=False);entries=[]
    for name,source in sorted(choices.items()):
        target=destination/name;target.parent.mkdir(parents=True,exist_ok=True);data=source.read_bytes();target.write_bytes(data)
        entries.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
    (destination/'public-source.json').write_text(json.dumps({'schema_version':1,'license':'MIT','history_included':False,
        'datasets_included':False,'weights_included':False,'files':entries},indent=2),encoding='utf-8')
    report=inspect(destination,True);report['destination']=str(destination);return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['export','check']);p.add_argument('--destination',type=Path);p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--verify-manifest',action='store_true');a=p.parse_args()
    if a.action=='export':
        if not a.destination:p.error('--destination is required')
        report=export(a.destination)
    else:report=inspect(a.root,a.verify_manifest)
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
