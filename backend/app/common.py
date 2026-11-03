"""【学习标注：通用业务工具】统一抛错、读取记录、过滤敏感字段、审计、通知和分页。"""
from fastapi import HTTPException
from sqlalchemy import select

from .models import Audit, Notification, Job


def require(condition, message="无权执行此操作", code=403):
    if not condition:
        raise HTTPException(code, message)


def fetch(db, model, identifier, lock=False):
    query = select(model).where(model.id == identifier)
    if lock:
        # SELECT FOR UPDATE 必须刷新会话内已经读过的对象，否则身份映射仍可能保留旧权限/状态。
        query = query.execution_options(populate_existing=True).with_for_update()
    obj = db.scalar(query)
    require(obj is not None, "记录不存在", 404)
    if model is Job:
        from .job_payloads import hydrate_job
        hydrate_job(db,obj)
    return obj


def public_row(obj, exclude=()):
    if isinstance(obj,Job):
        from .job_payloads import public_hydrate
        public_hydrate(obj)
    hidden = {"password_hash", "mfa_secret", "mfa_last_counter", "mfa_recovery_hashes", "token_hash", "object_key", "storage_upload_id", "dispatch_token", "delivery_token", "finalize_token"}
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in hidden | set(exclude)}


def audit(db, user, action, resource=None, space=None, **details):
    db.add(Audit(actor_id=user.id if user else None, action=action,
                 resource_id=resource, space_id=space, details=details))


def notify(db, user_id, title, body=""):
    db.add(Notification(user_id=user_id, title=title, body=body))


def page(items, offset=0, limit=50):
    return {"items": items[offset:offset + limit], "total": len(items)}
