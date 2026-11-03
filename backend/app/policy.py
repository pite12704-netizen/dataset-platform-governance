"""【学习标注：授权策略】集中回答“谁能发现、查看版本、读取内容、审批和管理”。

学习时优先读 discover、can_view_version、can_read_content；多数接口最终都调用这三层。
"""
from sqlalchemy import select, or_, and_

from .db import now
from .models import Acceptance, Dataset, Grant, Membership, Project, ProjectMember, Version


def member(db, user, space_id):
    if not user:
        return None
    return db.scalar(select(Membership).where(Membership.user_id == user.id,
                     Membership.space_id == space_id, Membership.active.is_(True)))


def space_admin(db, user, space_id):
    m = member(db, user, space_id)
    return bool(m and m.role == "admin")


def project_member(db, user, project_id, apply=False):
    if not user:
        return False
    p = db.get(Project, project_id)
    if not p or not member(db, user, p.space_id):
        return False
    pm = db.scalar(select(ProjectMember).where(ProjectMember.user_id == user.id,
                   ProjectMember.project_id == project_id, ProjectMember.active.is_(True)))
    return bool(pm and (not apply or pm.can_apply))


def manage_dataset(db, user, ds):
    return bool(user and ds.owner_id == user.id and member(db, user, ds.space_id))


def published(db, ds):
    return db.scalar(select(Version.id).where(Version.dataset_id == ds.id, Version.status == "published")) is not None


def discover(db, user, ds, settings):
    if manage_dataset(db, user, ds):
        return True
    if ds.status != "active" or not published(db, ds):
        return False
    if ds.visibility == "public":
        return settings.deployment == "public"
    if not user:
        return False
    if ds.visibility == "organization":
        return bool(member(db, user, ds.space_id))
    if ds.visibility == "selected":
        return user.id in ds.audience_users or any(project_member(db, user, p) for p in ds.audience_projects)
    return False


def review_publication(db, user, ds):
    if not user:
        return False
    if ds.visibility == "public":
        return user.platform_admin
    m = member(db, user, ds.space_id)
    return bool(m and m.role == "admin")


def active_grant(db, user, version):
    if not user:
        return None
    project=select(ProjectMember.id).join(Project,ProjectMember.project_id==Project.id).join(Membership,
        and_(Membership.space_id==Project.space_id,Membership.user_id==user.id,Membership.active.is_(True))).where(
        ProjectMember.project_id==Grant.project_id,ProjectMember.user_id==user.id,ProjectMember.active.is_(True)).exists()
    return db.scalar(select(Grant).where(Grant.version_id==version.id,Grant.revoked_at.is_(None),Grant.expires_at>now(),
        or_(Grant.user_id==user.id,project)).limit(1))


def can_read_content(db, user, version, settings, require_acceptance=True):
    ds = db.get(Dataset, version.dataset_id)
    if not user or not user.active or not user.verified or user.must_change_password or ds.status == "blocked":
        return False
    if manage_dataset(db, user, ds):
        return True
    if version.status != "published":
        return False
    accepted = db.scalar(select(Acceptance.id).where(Acceptance.user_id == user.id,
                         Acceptance.version_id == version.id))
    if require_acceptance and not accepted:
        return False
    if ds.access_mode == "open" and discover(db, user, ds, settings):
        return True
    return active_grant(db, user, version) is not None


def can_view_version(db, user, version, settings):
    ds = db.get(Dataset, version.dataset_id)
    return (manage_dataset(db, user, ds) or
            (version.status == "published" and (discover(db, user, ds, settings) or
             can_read_content(db, user, version, settings, require_acceptance=False))))
