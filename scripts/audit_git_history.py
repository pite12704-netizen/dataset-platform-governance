"""Read-only history inspection; reports locations/reasons, never matched values."""
import argparse,json,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import audit_repository as audit

def inspect(root):
    root=Path(root)
    def git(*args):
        p=subprocess.run(['git','-C',str(root),*args],capture_output=True)
        if p.returncode:raise RuntimeError('Cannot inspect Git history')
        return p.stdout
    commits=git('rev-list','--all').decode().splitlines();seen=set();findings=[];blobs=0
    secrets=audit.local_secrets()
    for commit in commits:
        for entry in git('ls-tree','-rz',commit).split(b'\0'):
            if not entry:continue
            meta,path=entry.split(b'\t',1);mode,kind,oid=meta.decode().split();path=path.decode('utf-8','replace')
            reason=audit.forbidden_path(path)
            if kind=='commit':reason='historical_gitlink'
            if mode=='120000':reason='historical_symlink'
            if reason:findings.append({'commit':commit[:12],'path':path,'reason':reason})
            if kind!='blob' or oid in seen:continue
            seen.add(oid);size=int(git('cat-file','-s',oid))
            if size>10*1024**2:
                findings.append({'commit':commit[:12],'path':path,'reason':'large_history_blob'});continue
            data=git('cat-file','blob',oid);blobs+=1
            if b'\0' in data:
                findings.append({'commit':commit[:12],'path':path,'reason':'binary_history_blob'});continue
            for item in audit.findings_for(path,data.decode('utf-8','replace'),secrets):findings.append({'commit':commit[:12],**item})
    return {'scope':'all-local-git-history','commits':len(commits),'unique_text_blobs_checked':blobs,'findings':findings,'passed':not findings}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=audit.ROOT);p.add_argument('--report',type=Path);a=p.parse_args()
    report=inspect(a.root)
    if a.report:a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='findings'}|{'finding_count':len(report['findings'])}))
    return 0 if report['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
