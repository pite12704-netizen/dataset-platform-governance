"""Fingerprint API build inputs without reading credentials, datasets or weights."""
import argparse
import hashlib
import json
from pathlib import Path

REQUIRED_FILES=(
    'backend/alembic.ini','backend/requirements.lock','clients/__init__.py',
    'clients/ingest/requirements.txt','clients/ingest/requirements.lock',
    'clients/ingest/source.example.json','clients/ingest/README.md',
    'converter_core.py','scripts/platform_data.py','scripts/build_fingerprint.py',
    'deployment/Dockerfile.api','.dockerignore')
PATTERNS=('backend/app/**/*.py','backend/migrations/**/*.py','backend/migrations/**/*.mako','clients/ingest/*.py')


def fingerprint(root):
    root=Path(root).resolve();paths={root/name for name in REQUIRED_FILES}
    for pattern in PATTERNS:paths.update(root.glob(pattern))
    if not all((root/name).is_file() for name in REQUIRED_FILES):raise RuntimeError('API build input is missing')
    digest=hashlib.sha256()
    for path in sorted(paths,key=lambda p:p.relative_to(root).as_posix()):
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise RuntimeError('Build input link escaped the source root')
        name=path.relative_to(root).as_posix()
        digest.update(name.encode()+b'\0'+hashlib.sha256(path.read_bytes()).digest())
    return {'format':1,'kind':'api','fingerprint':digest.hexdigest(),'files':len(paths)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path('.'))
    parser.add_argument('--output',type=Path);args=parser.parse_args();result=fingerprint(args.root)
    if args.output:args.output.write_text(json.dumps(result),encoding='utf-8')
    else:print(json.dumps(result))


if __name__=='__main__':main()
