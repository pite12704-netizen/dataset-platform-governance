"""真实数据迁移、加密备份与空库恢复；文件与元数据使用同一份校验清单。

迁移/恢复仅写空目标库，不删除源数据、不覆盖已有不同内容的对象。必须先停止
本项目 API/Worker/Beat；--quiesced 表明操作者已停止写入，不是在线一致性保证。
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import func, select
from sqlalchemy import DateTime, Boolean
from sqlalchemy import MetaData, Table, inspect, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from alembic import command
from alembic.config import Config
from app import models  # register schema
from app.config import Settings
from app.db import Base, Database, now
from app.storage import make_storage

MAGIC = b"DPBACKUP1"


def encode(value):
    if isinstance(value, datetime):
        return {"__dp_datetime__": value.isoformat()}
    raise TypeError(type(value).__name__)


def decode(value):
    if set(value) == {"__dp_datetime__"}:
        return datetime.fromisoformat(value["__dp_datetime__"])
    return value


def decode_manifest(raw):
    manifest = json.loads(raw)
    # 只解释声明为时间的数据库列；用户 JSON 内容不能改变反序列化类型。
    for name, rows in manifest.get("tables", {}).items():
        table = Base.metadata.tables.get(name)
        if table is None:
            raise RuntimeError("Unknown table in backup")
        for row in rows:
            for column in table.columns:
                if isinstance(column.type, DateTime) and row.get(column.name) is not None:
                    row[column.name] = decode(row[column.name])
    return manifest


def upgrade_manifest(manifest):
    """Verify the original manifest, then add deterministic defaults introduced after v1."""
    if hashlib.sha256(canonical(manifest["tables"])).hexdigest() != manifest["metadata_sha256"]:
        raise RuntimeError("Metadata checksum failed")
    if manifest['schema_version'] in {1, 2, 3, 4, 5, 6, 7, 8,9,10}:
        # Supported historical MySQL snapshots use reflected TINYINT columns.
        # Verify original bytes first, then normalize only declared Boolean fields.
        # This also covers e63/v8, not only the first reflected v3 release.
        for name, rows in manifest['tables'].items():
            for column in Base.metadata.tables[name].columns:
                if isinstance(column.type, Boolean):
                    for row in rows:
                        value = row.get(column.name)
                        if value is not None:
                            if type(value) not in {int, bool} or value not in {0, 1}:
                                raise RuntimeError('Invalid legacy boolean')
                            row[column.name] = bool(value)
    if manifest["schema_version"] == 1:
        statuses = {}
        for item in manifest["tables"].get("image_review_items", []):
            statuses.setdefault(item["batch_id"], []).append(item["status"])
        for batch in manifest["tables"].get("image_review_batches", []):
            values = statuses.get(batch["id"], [])
            batch["status"] = ("completed" if values and all(x == "approved" for x in values)
                               else "in_review" if any(x != "pending" for x in values) else "draft")
            batch["submitted_by_id"] = None
            batch["submitted_at"] = None
        manifest["schema_version"] = 2
        manifest["metadata_sha256"] = hashlib.sha256(canonical(manifest["tables"])).hexdigest()
    if manifest["schema_version"] == 2:
        for item in manifest["tables"].get("image_review_items", []):
            item.update(claimed_by_id=None, claim_token=None, claim_expires_at=None)
        manifest["schema_version"] = 3
    if manifest['schema_version']==3:
        from app.review_coverage import restored_pages
        for row in manifest['tables'].get('agent_conversations',[]):
            row.update(active_run_id=None,active_until=None,revision=0)
        for row in manifest['tables'].get('agent_runs',[]):row['request_id']=None
        manifest['tables']['image_review_images']=restored_pages(manifest['tables'])
        for name in ('vision_checkpoints','image_review_image_events','governance_reports'):
            manifest['tables'][name]=[]
        manifest['schema_version']=4
    if manifest['schema_version'] == 4:
        for name in ('import_batches', 'import_items'):
            manifest['tables'].setdefault(name, [])
        manifest['schema_version'] = 5
    if manifest['schema_version'] == 5:
        for row in manifest['tables'].get('users', []):
            row.setdefault('username', None); row.setdefault('must_change_password', False)
        for row in manifest['tables'].get('mail_outbox', []):
            row.setdefault('delivery_mode', None); row.setdefault('last_error', None)
        manifest['schema_version'] = 6
    if manifest['schema_version']==6:
        for row in manifest['tables'].get('spaces',[]):row.setdefault('quality_policy',{})
        for row in manifest['tables'].get('versions',[]):row.setdefault('quality_tier','raw')
        for row in manifest['tables'].get('import_batches',[]):row.setdefault('resolution',{})
        manifest['schema_version']=7
    if manifest['schema_version']==7:
        for row in manifest['tables'].get('users',[]):row.setdefault('mfa_recovery_hashes',[])
        for row in manifest['tables'].get('login_sessions',[]):row.setdefault('recovery_only',False)
        for row in manifest['tables'].get('challenges',[]):row.setdefault('payload',{})
        for name in ('thumbnails','evaluation_sets'):manifest['tables'].setdefault(name,[])
        manifest['schema_version']=8
    if manifest['schema_version']==8:
        manifest['tables'].setdefault('control_locks',[])
        manifest['tables'].setdefault('space_transfers',[])
        for row in manifest['tables'].get('jobs',[]):
            row.update(dispatch_token=None,next_dispatch_at=None,dispatch_attempts=0)
        for row in manifest['tables'].get('mail_outbox',[]):
            row.update(delivery_token=None,lease_until=None,next_attempt_at=None)
        for row in manifest['tables'].get('uploads',[]):row.update(finalize_token=None,finalize_until=None)
        manifest['schema_version']=9
    if manifest['schema_version'] in {9,10}:
        from app.job_payloads import split
        payloads={row['id']:row for row in manifest['tables'].get('job_payloads',[])}
        for job in manifest['tables'].get('jobs',[]):
            for field in ('parameters','result'):
                hot,records=split(job['id'],field,job.get(field) or {},job.get('created_at') or datetime(1970,1,1))
                job[field]=hot
                for record in records:payloads[record['id']]=record
        manifest['tables']['job_payloads']=list(payloads.values())
        manifest['schema_version']=10
    if manifest['schema_version']==10:
        manifest['tables'].setdefault('duplicate_pair_reviews',[])
        manifest['schema_version']=11
    if manifest["schema_version"] != 11:
        raise RuntimeError("Unsupported backup schema version")
    # Restore must not resurrect sessions, used recovery codes or reset links from an old backup.
    for row in manifest['tables'].get('login_sessions',[]):row['revoked']=True
    for row in manifest['tables'].get('service_credentials',[]):row['revoked']=True
    for row in manifest['tables'].get('challenges',[]):row['consumed']=True
    for row in manifest['tables'].get('users',[]):row['mfa_recovery_hashes']=[]
    for row in manifest['tables'].get('mail_outbox',[]):
        if not row.get('sent_at'):row.update(body='[恢复后已作废，请重新申请邮件]',attempts=5,last_error='RestoreInvalidated')
    # 领取是短期工作状态，备份恢复不能复活旧浏览器的凭证或阻塞新审核人。
    for item in manifest["tables"].get("image_review_items", []):
        item.update(claimed_by_id=None, claim_token=None, claim_expires_at=None)
    for row in manifest['tables'].get('image_review_images',[]):
        row.update(claimed_by_id=None,claim_token=None,claim_expires_at=None)
    for row in manifest['tables'].get('agent_conversations',[]):row.update(active_run_id=None,active_until=None)
    for row in manifest['tables'].get('import_batches', []):row['revoked'] = True
    for row in manifest['tables'].get('jobs',[]):row.update(dispatch_token=None,next_dispatch_at=None,dispatch_attempts=0)
    for row in manifest['tables'].get('uploads',[]):
        row.update(finalize_token=None,finalize_until=None)
        if row.get('status')=='finalizing':row['status']='uploading'
    for row in manifest['tables'].get('mail_outbox',[]):row.update(delivery_token=None,lease_until=None,next_attempt_at=None)
    for row in manifest['tables'].get('space_transfers',[]):
        if row['status']=='pending':row.update(status='cancelled',decided_at=now())
    from app.job_payloads import validate_backup_payloads
    validate_backup_payloads(manifest['tables'])
    from app.joint_review import validate_backup
    validate_backup(manifest['tables'])
    for rows in manifest['tables'].values():rows.sort(key=lambda row:row['id'])
    manifest["metadata_sha256"] = hashlib.sha256(canonical(manifest["tables"])).hexdigest()
    return manifest


def canonical(value):
    return json.dumps(value, default=encode, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def migrate_schema(settings):
    cfg = Config(str(ROOT / "backend/alembic.ini"))
    cfg.attributes["settings"] = settings
    command.upgrade(cfg, "head")


def snapshot(database,legacy_schema=False):
    legacy_revision = None
    metadata_bytes=0
    with database.engine.connect() as conn:
        if conn.dialect.name == "mysql":
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            tables = {}
            source_tables=Base.metadata.sorted_tables
            known_legacy={'f2b8d527c4e3','a6c9e341d508','b9e81a7302fd','c41f8d29a705','d52a0f18b906','e63b2a09c817','f74c3d10e928','a85d4e21fa39'}
            if 'alembic_version' in inspect(conn).get_table_names():
                actual_revision=conn.scalar(text('SELECT version_num FROM alembic_version'))
                if actual_revision in known_legacy:legacy_schema=True
                elif actual_revision!='c63f8a29d105':raise RuntimeError('Unsupported source schema; refusing incomplete backup')
            if legacy_schema:
                revision=conn.scalar(text('SELECT version_num FROM alembic_version'))
                legacy_revision = revision
                if revision not in known_legacy:
                    raise RuntimeError('Legacy backup only supports explicitly identified previous releases')
                names=set(inspect(conn).get_table_names())-{'alembic_version'}
                excluded = {'duplicate_pair_reviews'}
                if revision!='a85d4e21fa39':excluded.add('job_payloads')
                if revision not in {'f74c3d10e928','a85d4e21fa39'}:excluded|={'control_locks','space_transfers'}
                if revision not in {'e63b2a09c817','f74c3d10e928','a85d4e21fa39'}:excluded|={'thumbnails','evaluation_sets'}
                if revision not in {'b9e81a7302fd','c41f8d29a705','d52a0f18b906','e63b2a09c817','f74c3d10e928','a85d4e21fa39'}:excluded|={'import_batches','import_items'}
                if revision=='f2b8d527c4e3':
                    excluded |= {'image_review_images','image_review_image_events','vision_checkpoints','governance_reports'}
                expected=set(Base.metadata.tables)-excluded
                if names!=expected:raise RuntimeError('Unexpected legacy tables; refusing partial backup')
                metadata=MetaData()
                source_tables=[Table(name,metadata,autoload_with=conn) for name in sorted(names)]
            for table in source_tables:
                omitted = ({'username', 'must_change_password'} if table.name == 'users' else
                           {'delivery_mode', 'last_error'} if table.name == 'mail_outbox' else set()) if legacy_revision in {'a6c9e341d508', 'b9e81a7302fd'} else set()
                if legacy_revision in {'a6c9e341d508','b9e81a7302fd'}:
                    omitted |= {'quality_policy'} if table.name=='spaces' else {'quality_tier'} if table.name=='versions' else {'resolution'} if table.name=='import_batches' else set()
                rows=[]
                for row in conn.execute(select(*[c for c in table.columns if c.name not in omitted])
                        .execution_options(yield_per=1000)).mappings():
                    item=dict(row);metadata_bytes+=len(canonical(item))
                    if metadata_bytes>database.backup_metadata_max_bytes:
                        raise RuntimeError('Metadata exceeds the bounded maintenance-backup budget; use a scalable database backup plan')
                    rows.append(item)
                tables[table.name] = sorted(rows, key=lambda row: row["id"])
    if any(j["status"] in {"queued", "running"} for j in tables["jobs"]):
        raise RuntimeError("Queued/running jobs remain; drain work before snapshot")
    if any(u["status"] in {"uploading","finalizing","aborting"} for u in tables["uploads"]):
        raise RuntimeError("Incomplete uploads remain; finish or abort before snapshot")
    if any(r['status']=='running' and r['created_at']>now()-timedelta(minutes=10)
           for r in tables.get('agent_runs',[])):
        raise RuntimeError('Active Agent queries remain; drain requests before snapshot')
    objects = {}
    for row in tables["assets"]:
        objects[row["object_key"]] = {"key": row["object_key"], "size": row["size"], "sha256": row["sha256"]}
    for row in tables.get('thumbnails',[]):
        objects[row['object_key']]={'key':row['object_key'],'size':row['size'],'sha256':row['sha256']}
    for job in tables["jobs"]:
        if job["artifact_bytes"]:
            key = f"packages/{job['id']}/{job['result']['artifact_id']}"
            objects[key] = {"key": key, "size": job["artifact_bytes"], "sha256": job['result'].get('sha256')}
    for key in objects:
        if not re.fullmatch(r"[A-Za-z0-9/_-]+", key):
            raise RuntimeError("Invalid internal object key")
    for item in tables.get("image_review_items", []):
        item.update(claimed_by_id=None, claim_token=None, claim_expires_at=None)
    for item in tables.get('image_review_images',[]):item.update(claimed_by_id=None,claim_token=None,claim_expires_at=None)
    for row in tables.get('agent_conversations',[]):row.update(active_run_id=None,active_until=None)
    for row in tables.get('import_batches', []):row['revoked'] = True
    if legacy_revision == 'f2b8d527c4e3':
        for row in tables.get('agent_conversations',[]):
            row.pop('active_run_id',None);row.pop('active_until',None)
    schema_version = 3 if legacy_revision == 'f2b8d527c4e3' else 4 if legacy_revision == 'a6c9e341d508' else 5 if legacy_revision == 'b9e81a7302fd' else 6 if legacy_revision=='c41f8d29a705' else 7 if legacy_revision=='d52a0f18b906' else 8 if legacy_revision=='e63b2a09c817' else 9 if legacy_revision=='f74c3d10e928' else 10 if legacy_revision=='a85d4e21fa39' else 11
    return {"schema_version": schema_version, "created_at": now().isoformat() + "Z", "tables": tables,
            'mfa_key_ids':list(database.mfa_key_ids),
            "objects": list(objects.values()), "metadata_sha256": hashlib.sha256(canonical(tables)).hexdigest()}


def read_checked(chunks, expected, out=None):
    digest, size = hashlib.sha256(), 0
    for chunk in chunks:
        size += len(chunk)
        if size > expected["size"]:
            raise RuntimeError("Object exceeds declared size")
        digest.update(chunk)
        if out:
            out.write(chunk)
    sha = digest.hexdigest()
    if size != expected["size"] or expected.get("sha256") and expected["sha256"] != sha:
        raise RuntimeError("Object integrity failed: " + expected["key"])
    return sha


def empty_target(database):
    with database.engine.connect() as conn:
        if any(conn.scalar(select(func.count()).select_from(t)) for t in Base.metadata.sorted_tables):
            raise RuntimeError("Target contains business data; refusing to overwrite")


def ordered(table, rows):
    # Version 的 parent_version_id 是自关联，必须先插入父版本。
    self_fields = [fk.parent.name for fk in table.foreign_keys if fk.column.table is table]
    if not self_fields:
        return rows
    pending, result, inserted = list(rows), [], set()
    while pending:
        ready = [row for row in pending if all(not row[f] or row[f] in inserted for f in self_fields)]
        if not ready:
            raise RuntimeError("Unresolved self references: " + table.name)
        result.extend(ready)
        inserted.update(row["id"] for row in ready)
        pending = [row for row in pending if row["id"] not in inserted]
    return result


def import_metadata(database, manifest):
    manifest = upgrade_manifest(manifest)
    if not set(manifest.get('mfa_key_ids',[]))<=set(database.mfa_key_ids):
        raise RuntimeError('Restore target is missing required MFA encryption key versions')
    if set(manifest["tables"]) != set(Base.metadata.tables):
        raise RuntimeError("Backup schema does not match this release")
    with database.engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            for row in ordered(table, manifest["tables"][table.name]):
                conn.execute(table.insert().values(**row))
    verified = snapshot(database)
    if verified["metadata_sha256"] != manifest["metadata_sha256"]:
        raise RuntimeError("Imported metadata differs; keep target offline for investigation")


def existing_object(storage, key):
    if hasattr(storage, "client"):
        from botocore.exceptions import ClientError
        try:
            storage.client.head_object(Bucket=storage.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
    return storage.path(key).exists()


def put_checked(storage, expected, stream):
    if existing_object(storage, expected["key"]):
        read_checked(storage.chunks(expected["key"]), expected)
        return
    storage.put_file(expected["key"], stream)
    read_checked(storage.chunks(expected["key"]), expected)


def key_bytes(path):
    key = bytes.fromhex(Path(path).read_text().strip())
    if len(key) != 32:
        raise RuntimeError("Backup key must contain 64 hex characters")
    return key


def encrypted_copy(source, destination, key):
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with Path(destination).open("xb") as out:
        out.write(MAGIC + nonce)
        while chunk := source.read(1024 * 1024):
            out.write(encryptor.update(chunk))
        out.write(encryptor.finalize())
        out.write(encryptor.tag)


def decrypt_backup(path, destination, key):
    with Path(path).open("rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise RuntimeError("Invalid backup format")
        nonce = source.read(12)
        source.seek(-16, io.SEEK_END)
        tag = source.read(16)
        remaining = source.tell() - 16 - len(MAGIC) - 12
        source.seek(len(MAGIC) + 12)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        while remaining > 0:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise RuntimeError("Truncated backup")
            remaining -= len(chunk)
            destination.write(decryptor.update(chunk))
        destination.write(decryptor.finalize())
    destination.seek(0)


def report(manifest, action, path):
    value = {"action": action, "verified": True, "metadata_sha256": manifest["metadata_sha256"],
             "tables": {name: len(rows) for name, rows in manifest["tables"].items()},
             "objects": len(manifest["objects"]), "bytes": sum(o["size"] for o in manifest["objects"])}
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps(value), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "backup", "restore", "inventory"])
    parser.add_argument("--source-env", default=".env")
    parser.add_argument("--target-env")
    parser.add_argument("--bundle")
    parser.add_argument("--key-file")
    parser.add_argument("--report")
    parser.add_argument("--quiesced", action="store_true")
    parser.add_argument('--legacy-schema',action='store_true',help='只对明确的上一发布版本执行完整反射备份/盘点')
    args = parser.parse_args()
    if args.action != "inventory" and not args.quiesced:
        parser.error("Stop project writers and pass --quiesced")
    source_cfg = Settings(_env_file=args.source_env)
    source_db = Database(source_cfg.database_url, source_cfg)
    storage = make_storage(source_cfg)
    target_cfg, target_db = None, None
    try:
        if args.action == "inventory":
            manifest = snapshot(source_db,args.legacy_schema)
            for item in manifest["objects"]:
                item["sha256"] = read_checked(storage.chunks(item["key"]), item)
            report(manifest, args.action, args.report)
            return
        target_cfg = None
        if args.action in {"migrate", "restore"}:
            if not args.target_env:
                parser.error("--target-env is required")
            target_cfg = Settings(_env_file=args.target_env)
            if source_cfg.secret_key != target_cfg.secret_key:
                raise RuntimeError("Application key must remain identical for encrypted identities")
            migrate_schema(target_cfg)
            target_db, target_storage = Database(target_cfg.database_url, target_cfg), make_storage(target_cfg)
            empty_target(target_db)
        if args.action == "migrate":
            manifest = snapshot(source_db)
            for item in manifest["objects"]:
                with tempfile.TemporaryFile() as staged:
                    item["sha256"] = read_checked(storage.chunks(item["key"]), item, staged)
                    staged.seek(0)
                    put_checked(target_storage, item, staged)
            import_metadata(target_db, manifest)
        elif args.action == "backup":
            if not args.bundle or not args.key_file:
                parser.error("--bundle and --key-file are required")
            manifest = snapshot(source_db,args.legacy_schema)
            manifest["application_key_sha256"] = hashlib.sha256(source_cfg.secret_key.encode()).hexdigest()
            with tempfile.TemporaryFile() as staged:
                with zipfile.ZipFile(staged, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for index, item in enumerate(manifest["objects"]):
                        item["member"] = f"objects/{index:08d}"
                        with archive.open(item["member"], "w", force_zip64=True) as entry:
                            item["sha256"] = read_checked(storage.chunks(item["key"]), item, entry)
                    archive.writestr("manifest.json", canonical(manifest))
                staged.seek(0)
                encrypted_copy(staged, args.bundle, key_bytes(args.key_file))
        else:
            if not args.bundle or not args.key_file:
                parser.error("--bundle and --key-file are required")
            with tempfile.TemporaryFile() as decrypted:
                decrypt_backup(args.bundle, decrypted, key_bytes(args.key_file))
                with zipfile.ZipFile(decrypted) as archive:
                    if archive.getinfo("manifest.json").file_size > 256 * 1024**2:
                        raise RuntimeError("Manifest exceeds limit")
                    manifest = upgrade_manifest(decode_manifest(archive.read("manifest.json")))
                    if manifest["application_key_sha256"] != hashlib.sha256(target_cfg.secret_key.encode()).hexdigest():
                        raise RuntimeError("Application key differs from backup")
                    for item in manifest["objects"]:
                        if not re.fullmatch(r"[A-Za-z0-9/_-]+", item["key"]):
                            raise RuntimeError("Invalid object key in manifest")
                        with tempfile.TemporaryFile() as staged, archive.open(item["member"]) as original:
                            read_checked(iter(lambda: original.read(1024 * 1024), b""), item, staged)
                            staged.seek(0)
                            put_checked(target_storage, item, staged)
            import_metadata(target_db, manifest)
        report(manifest, args.action, args.report)
    finally:
        source_db.engine.dispose()
        if target_db:
            target_db.engine.dispose()


if __name__ == "__main__":
    main()
