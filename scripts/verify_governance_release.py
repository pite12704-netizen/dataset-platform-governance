"""本轮只读验证及隔离恢复演练；不会清空、覆盖或服务化恢复后的业务数据。"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'scripts')]
from app.config import Settings
from app.db import Database
from app.storage import make_storage
from sqlalchemy.engine import make_url
from platform_data import canonical,decode_manifest,decrypt_backup,key_bytes,upgrade_manifest,snapshot
from platform_data import read_checked,put_checked,migrate_schema,empty_target,import_metadata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore-drill',action='store_true')
    args=parser.parse_args()
    source=Settings(_env_file=ROOT/'.env.business.local')
    source_db=Database(source.database_url,source)
    backup=ROOT/'var/operations/before-governance-20261008.dpbackup'
    evidence={}
    try:
        actual=snapshot(source_db)
        with tempfile.TemporaryFile() as decrypted:
            decrypt_backup(str(backup),decrypted,key_bytes(str(ROOT/'deployment/secrets/backup_key')))
            with zipfile.ZipFile(decrypted) as archive:
                manifest=decode_manifest(archive.read('manifest.json'))
                assert hashlib.sha256(canonical(manifest['tables'])).hexdigest()==manifest['metadata_sha256']
                for item in manifest['objects']:
                    with archive.open(item['member']) as stream:
                        read_checked(iter(lambda:stream.read(1024*1024),b''),item)
                upgraded=upgrade_manifest(manifest)
                assert actual['metadata_sha256']==upgraded['metadata_sha256'],'Migration changed existing business records unexpectedly'
                assert {o['key']:(o['size'],o['sha256']) for o in actual['objects']}=={
                    o['key']:(o['size'],o['sha256']) for o in manifest['objects']}
                assert all(p['status']=='pending' for p in actual['tables']['image_review_images'])
                evidence.update(migration_verified=True,old_records_preserved=True,objects=len(actual['objects']),
                    bytes=sum(o['size'] for o in actual['objects']),new_image_pending=len(actual['tables']['image_review_images']),
                    metadata_sha256=actual['metadata_sha256'],backup_authenticated=True,
                    backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
                if args.restore_drill:
                    integration=Settings(_env_file=ROOT/'.env.integration.local',environment='test')
                    url=make_url(integration.database_url)
                    assert url.username=='dp_test' and url.database=='dataset_platform_integration'
                    database_name='dataset_platform_governance_restore_'+uuid.uuid4().hex[:8]
                    assert re.fullmatch('dataset_platform_governance_restore_[a-f0-9]{8}',database_name)
                    compose=['docker','compose','--env-file','deployment/compose.env.local','-f','deployment/compose.yml',
                        '-f','deployment/compose.local.yml','-f','deployment/compose.desktop.local']
                    sql=f'CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4; GRANT ALL ON `{database_name}`.* TO \'dp_test\'@\'%\';'
                    subprocess.run(compose+['exec','-T','mysql','sh','-c',
                        'MYSQL_PWD="$(cat /run/secrets/mysql_root_password)" exec mysql -u root'],input=sql,text=True,check=True,cwd=ROOT)
                    target=source.model_copy(update={'environment':'test','database_url':url.set(database=database_name).render_as_string(hide_password=False),
                        'storage_backend':'local','storage_root':ROOT/'var/run'/database_name/'objects','task_mode':'manual','scan_mode':'development'})
                    migrate_schema(target);target_db=Database(target.database_url,target);target_storage=make_storage(target)
                    try:
                        empty_target(target_db)
                        for item in upgraded['objects']:
                            with tempfile.TemporaryFile() as staged,archive.open(item['member']) as stream:
                                read_checked(iter(lambda:stream.read(1024*1024),b''),item,staged)
                                staged.seek(0);put_checked(target_storage,item,staged)
                        import_metadata(target_db,upgraded)
                        restored=snapshot(target_db)
                        assert restored['metadata_sha256']==actual['metadata_sha256']
                        for item in restored['objects']:read_checked(target_storage.chunks(item['key']),item)
                        evidence.update(restore_drill_verified=True,restored_test_database=database_name,
                            restore_destination='isolated MySQL dp_test + local objects; no API running')
                    finally:target_db.engine.dispose()
        (ROOT/'var/operations/governance-migration-validation.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(evidence,ensure_ascii=False))
    finally:source_db.engine.dispose()

if __name__=='__main__':main()
