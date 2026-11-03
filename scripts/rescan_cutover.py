"""迁移后的安全复检：旧开发扫描结果重新进入隔离区，由真实 ClamAV Worker 扫描。"""
import sys
from pathlib import Path
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import Settings
from app.db import Database
from app.models import Asset, Audit, Dataset, Job, Version

settings = Settings(_env_file=ROOT / ".env.business.local")
if settings.storage_backend != "s3" or not settings.database_url.startswith("mysql") or settings.scan_mode != "clamav":
    raise RuntimeError("Rescan requires MySQL + S3 + ClamAV")
database = Database(settings.database_url, settings)
count = 0
with database.Session() as db:
    for asset in db.scalars(select(Asset)):
        if asset.checks.get("malware_method") == "clamav":
            continue
        version = db.get(Version, asset.version_id)
        dataset = db.get(Dataset, version.dataset_id)
        key = "security-cutover-v1:" + asset.id
        if db.scalar(select(Job.id).where(Job.user_id == dataset.owner_id, Job.idempotency_key == key)):
            continue
        asset.status = "quarantine"
        db.add(Job(space_id=dataset.space_id, user_id=dataset.owner_id, version_id=version.id,
                   kind="scan", parameters={"asset_id": asset.id}, idempotency_key=key))
        db.add(Audit(actor_id=None, space_id=dataset.space_id, action="asset.security_recheck",
                     resource_id=asset.id, details={"reason": "switch_to_clamav"}))
        count += 1
    db.commit()
database.engine.dispose()
print(f"Queued {count} files for actual malware revalidation; originals retained.")
