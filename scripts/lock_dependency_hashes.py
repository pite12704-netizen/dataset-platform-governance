"""Bind already-pinned Python versions to published PyPI file hashes; never upgrade."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote
from urllib.request import Request,urlopen


def pins(source):
    result=[]
    for line in source.splitlines():
        value=line.strip().removesuffix('\\').strip()
        if not value or value.startswith('#'):continue
        if re.fullmatch(r'--hash=sha256:[a-f0-9]{64}',value):continue
        match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)',value)
        if not match:raise ValueError('Unsupported or unpinned dependency syntax')
        result.append(match.groups())
    if not result or len({name.lower() for name,_ in result})!=len(result):raise ValueError('Empty or duplicate dependency pins')
    return result


def metadata(item):
    name,version=item
    request=Request('https://pypi.org/pypi/'+quote(name,safe='')+'/'+quote(version,safe='')+'/json',
                    headers={'User-Agent':'dataset-platform-dependency-lock'})
    with urlopen(request,timeout=20) as response:return json.load(response)


def render(source,fetch=metadata):
    requirements=pins(source)
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(fetch,requirements))
    output=['# Version pins and multi-platform file hashes from PyPI. Do not edit hashes manually.']
    total=0
    normalize=lambda value:re.sub(r'[-_.]+','-',value).lower()
    for (name,version),data in zip(requirements,results):
        if normalize(data['info']['name'])!=normalize(name) or data['info']['version']!=version:
            raise ValueError('Dependency metadata identity mismatch')
        if any(not advisory.get('withdrawn') for advisory in data.get('vulnerabilities',[])):
            raise ValueError('Pinned package has unresolved published advisories; review versions first')
        hashes=sorted({row['digests']['sha256'] for row in data['urls'] if not row.get('yanked')})
        if not hashes or not all(re.fullmatch('[a-f0-9]{64}',value) for value in hashes):raise ValueError('Missing verified dependency file hashes')
        output.append(name+'=='+version+' \\')
        output.extend('    --hash=sha256:'+value+(' \\' if index<len(hashes)-1 else '') for index,value in enumerate(hashes))
        total+=len(hashes)
    return '\n'.join(output)+'\n',{'packages':len(requirements),'file_hashes':total,'unresolved_pypi_advisories':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--lock',type=Path,default=Path('backend/requirements.lock'))
    parser.add_argument('--report',type=Path);args=parser.parse_args()
    original=args.lock.read_bytes();source,report=render(original.decode('utf-8'))
    if args.lock.read_bytes()!=original:raise RuntimeError('Dependency lock changed during verification')
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=args.lock.resolve().parent,
                prefix='.dependency-lock-',delete=False) as stream:
            temporary=Path(stream.name);stream.write(source)
        os.replace(temporary,args.lock)
    finally:
        if temporary and temporary.exists():temporary.unlink()
    if args.report:args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
