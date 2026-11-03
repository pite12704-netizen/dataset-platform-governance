"""Account provisioning and serialized platform-wide logical storage admission.

Lock order for growth: Dataset -> Version -> Job (if any) -> ControlLock -> Space.
Releases do not acquire the sentinel; concurrent decreases remain safe.
"""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .common import fetch, require
from .models import ControlLock, Space, User


def lock(db, name):
    if db.get(ControlLock, name) is None:
        try:
            with db.begin_nested():
                db.add(ControlLock(id=name))
                db.flush()
        except IntegrityError:
            pass  # Another transaction installed the same sentinel.
    return fetch(db, ControlLock, name, lock=True)


def account_provision(db, user_id, requested, settings, organization=False):
    fetch(db, User, user_id, lock=True)
    count, allocated = db.execute(select(func.count(), func.coalesce(func.sum(Space.quota_bytes), 0))
        .where(Space.owner_id == user_id, Space.kind == 'organization')).one()
    if organization:
        require(count < settings.account_max_organizations, '账号可创建的组织数量已达上限', 409)
    if requested <= 0:
        return  # Legacy accounts over budget must still be able to reduce allocations.
    personal = db.scalar(select(func.coalesce(func.sum(Space.quota_bytes), 0))
        .where(Space.owner_id == user_id, Space.kind != 'organization'))
    require(allocated + personal + requested <= settings.account_quota_budget_bytes,
            '账号已分配空间配额超过总预算，请联系管理员', 409)


def growth(db, amount, settings):
    if amount <= 0:
        return
    lock(db, 'storage-growth')
    total = db.scalar(select(func.coalesce(func.sum(Space.used_bytes + Space.reserved_bytes), 0)))
    require(total + amount <= settings.platform_storage_budget_bytes,
            '平台逻辑存储预算不足，暂不能增加存储；现有读取不受影响', 409)
