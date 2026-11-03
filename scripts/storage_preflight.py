"""Host disk admission before application start/build; never starts Docker or deletes data."""
import argparse
import json
import os
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]


def check(paths,minimum_gib=10,usage=shutil.disk_usage):
    if minimum_gib<1:raise ValueError('Minimum free space must be at least 1GiB')
    rows=[];seen=set()
    for value in paths:
        path=Path(value).resolve()
        while not path.exists() and path.parent!=path:path=path.parent
        anchor=path.anchor or str(path)
        identity=anchor if os.name=='nt' else path.stat().st_dev
        if identity in seen:continue
        seen.add(identity)
        if os.name!='nt':
            mount=path if path.is_dir() else path.parent
            while mount.parent!=mount and mount.parent.stat().st_dev==identity:mount=mount.parent
            anchor=str(mount)
        free=usage(path).free
        rows.append({'volume':anchor,'free_gib':round(free/1024**3,2),'minimum_gib':minimum_gib,'sufficient':free>=minimum_gib*1024**3})
    return {'allowed':all(row['sufficient'] for row in rows),'checked_volumes':rows,'automatic_cleanup':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',action='append',default=[])
    parser.add_argument('--minimum-gib',type=int,default=10)
    args=parser.parse_args()
    maintenance=ROOT/'var/run/storage-recovery-required.json'
    if maintenance.exists():
        print(json.dumps({'allowed':False,'maintenance_block':True,'automatic_cleanup':False,
            'reason':'Production recovery acceptance and the new task-payload release are still pending.'}))
        print('Keep the original incident disk offline. A verified work copy does not authorize production switch; complete release acceptance before removing the maintenance marker.')
        raise SystemExit(3)
    paths=[ROOT,*args.path]
    if os.name=='nt':
        paths.append(Path(os.environ.get('SystemDrive','C:')+'\\'))
        default=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Docker/wsl/disk/docker_data.vhdx'
        paths.append(os.environ.get('DP_DOCKER_DATA_DISK',str(default)))
    elif Path('/var/lib/docker').exists():paths.append(Path('/var/lib/docker'))
    try:result=check(paths,args.minimum_gib)
    except (OSError,ValueError):
        print('Cannot verify host disk free space; no services were changed.');raise SystemExit(3)
    print(json.dumps(result))
    if not result['allowed']:
        print('Insufficient host disk space. Keep Docker stopped; protect data and resolve storage capacity before starting or building.')
        raise SystemExit(3)


if __name__=='__main__':main()
