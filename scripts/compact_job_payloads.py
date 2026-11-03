"""Preview/apply legacy hot-row compaction during an explicitly approved maintenance window.

Default is read-only. --apply also requires --quiesced and the new schema; this
does not migrate, retry, cancel or mark jobs successful, and never purges logs.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import String,cast,func,or_,select,text
from sqlalchemy.orm.attributes import flag_dirty
from app.config import Settings
from app.db import Database
from app.models import Job
from app.job_payloads import HOT_BYTES,INLINE_BYTES


def compact(settings,apply=False,quiesced=False):
    if apply and not quiesced:raise ValueError('Stop all application writers and pass --quiesced before applying')
    database=Database(settings.database_url,settings)
    lengths=[func.length(cast(Job.parameters,String)),func.length(cast(Job.result,String))]
    candidate=or_(*(value>INLINE_BYTES for value in lengths))
    try:
        with database.Session() as db:
            schema=db.scalar(text('SELECT version_num FROM alembic_version'))
            if schema not in {'f74c3d10e928','a85d4e21fa39','c63f8a29d105'}:raise ValueError('Unreviewed source schema')
            count=db.scalar(select(func.count()).select_from(Job).where(candidate))
            raw=db.scalar(select(func.coalesce(func.sum(lengths[0]+lengths[1]),0)).where(candidate))
        report={'mode':'apply' if apply else 'preview','schema':schema,'candidates':count,'legacy_json_bytes':int(raw or 0),
            'jobs_visited':0,'jobs_cancelled':0,'logs_purged':False,'business_state_changed':False}
        if not apply:return report
        if schema not in {'a85d4e21fa39','c63f8a29d105'}:raise ValueError('Create the payload table using the approved migration before applying')
        last=''
        while True:
            with database.Session() as db:
                job=db.scalar(select(Job).where(candidate,Job.id>last).order_by(Job.id).limit(1).with_for_update())
                if job is None:break
                last=job.id;flag_dirty(job);db.commit();report['jobs_visited']+=1
        with database.Session() as db:
            remaining=db.scalar(select(func.count()).select_from(Job).where(or_(*(value>HOT_BYTES for value in lengths))))
            if remaining:raise RuntimeError('Some hot rows still exceed the safety bound; do not start workers')
        report['hot_rows_bounded']=True
        return report
    finally:database.engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--apply',action='store_true')
    parser.add_argument('--quiesced',action='store_true');parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.report:
        if args.report.exists():raise ValueError('Choose a new report path; existing operation records are never overwritten')
        args.report.parent.mkdir(parents=True,exist_ok=True)
    settings=Settings(_env_file=args.env_file)
    result=compact(settings,args.apply,args.quiesced)
    if args.report:args.report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
