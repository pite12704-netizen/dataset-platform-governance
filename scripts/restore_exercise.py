"""Restore an encrypted backup only into a newly allocated local exercise namespace."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
sys.path.insert(0,str(ROOT))


def exercise(bundle,backup_key_file,app_key_file,max_bytes=10*1024**3,mfa_keyring_file=None,max_metadata_bytes=128*1024**2):
    from scripts.platform_data import decrypt_backup,key_bytes,decode_manifest,upgrade_manifest,migrate_schema,empty_target,put_checked,import_metadata,read_checked
    from app.config import Settings
    from app.db import Database
    from app.storage import make_storage
    directory=ROOT/'var/test-runs'/('restore-'+uuid.uuid4().hex);directory.mkdir(parents=True,exist_ok=False)
    secret=Path(app_key_file).read_text(encoding='utf-8').strip()
    cfg=Settings(_env_file=None,_env_prefix='RESTORE_EXERCISE_UNUSED_',environment='test',secret_key=secret,
        mfa_encryption_keys_file=mfa_keyring_file,
        database_url='sqlite:///'+str(directory/'restored.db'),storage_backend='local',storage_root=directory/'objects',
        model_url='',vision_model_url='',smtp_host='',task_mode='manual',scan_mode='development',
        backup_metadata_max_bytes=max_metadata_bytes)
    db=Database(cfg.database_url,cfg);storage=make_storage(cfg)
    try:
        with tempfile.TemporaryFile() as decrypted:
            decrypt_backup(bundle,decrypted,key_bytes(backup_key_file))
            with zipfile.ZipFile(decrypted) as archive:
                if archive.getinfo('manifest.json').file_size>256*1024**2:raise RuntimeError('Backup manifest exceeds limit')
                manifest=upgrade_manifest(decode_manifest(archive.read('manifest.json')))
                if manifest['application_key_sha256']!=hashlib.sha256(secret.encode()).hexdigest():raise RuntimeError('Application key mismatch')
                total=sum(item['size'] for item in manifest['objects'])
                if total>max_bytes or total>shutil.disk_usage(directory).free:raise RuntimeError('Exercise storage budget insufficient')
                migrate_schema(cfg);empty_target(db)
                for item in manifest['objects']:
                    with archive.open(item['member']) as stream:put_checked(storage,item,stream)
                import_metadata(db,manifest)
                for item in manifest['objects']:read_checked(storage.chunks(item['key']),item)
        result={'isolated':True,'business_services_accessed':False,'objects_verified':len(manifest['objects']),
            'bytes_verified':total,'tables_verified':len(manifest['tables']),'schema_version':manifest['schema_version'],
            'metadata_budget_bytes':cfg.backup_metadata_max_bytes,
            'limitations':['Local SQLite/object restoration; repeat on a new MySQL/S3 target for production recovery certification']}
        (directory/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return directory/'report.json',result
    finally:storage.close();db.engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bundle',required=True);parser.add_argument('--backup-key-file',required=True);parser.add_argument('--app-key-file',required=True)
    parser.add_argument('--max-bytes',type=int,default=10*1024**3);parser.add_argument('--mfa-keyring-file')
    parser.add_argument('--max-metadata-bytes',type=int,default=128*1024**2,help='Explicit metadata budget, 1024 bytes to 1 GiB; default 128 MiB')
    args=parser.parse_args()
    path,result=exercise(args.bundle,args.backup_key_file,args.app_key_file,args.max_bytes,args.mfa_keyring_file,args.max_metadata_bytes)
    print(json.dumps({'report':str(path),**result},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception:print('Restore exercise failed; no existing target database or bucket was used.',file=sys.stderr);raise SystemExit(1)
