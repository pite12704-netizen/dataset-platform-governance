"""【学习标注：组织与项目】管理空间成员、组织角色、项目及项目申请资格。

组织成员资格决定可见范围；项目成员的 can_apply 决定能否代表项目申请数据。
"""
from fastapi import APIRouter, Depends, Request
from pydantic import Field
from typing import Literal
from sqlalchemy import or_, select

from . import schemas as s
from .common import audit, fetch, notify, public_row, require
from .db import get_db
from .models import Dataset, Membership, Project, ProjectMember, Space, User
from .policy import member, space_admin
from .security import interactive_user

router = APIRouter(prefix="/spaces", tags=["组织与项目"])


class QualityPolicyInput(s.Input):
    revision: int = Field(ge=0)
    minimum_tier: Literal['raw','curated','certified'] = 'raw'
    require_blind_evaluation: bool = False
    min_truth_images: int = Field(default=100,ge=1,le=10000)
    min_positives: int = Field(default=20,ge=1,le=10000)
    min_negatives: int = Field(default=20,ge=0,le=10000)
    min_precision: float = Field(default=.9,ge=0,le=1)
    min_recall: float = Field(default=.8,ge=0,le=1)
    min_attribute_coverage: float = Field(default=.95,ge=0,le=1)


@router.put('/{space_id}/quality-policy')
def quality_policy(space_id:str,data:QualityPolicyInput,db=Depends(get_db),user=Depends(interactive_user)):
    space=fetch(db,Space,space_id,lock=True)
    require(space_admin(db,user,space.id),'需要组织管理员设置质量策略')
    require(data.revision==(space.quality_policy or {}).get('revision',0),'策略已被修改，请刷新',409)
    space.quality_policy={**data.model_dump(),'revision':data.revision+1}
    audit(db,user,'space.quality_policy',space.id,space.id,policy=space.quality_policy)
    db.commit()
    return space.quality_policy


@router.get("")
def list_spaces(db=Depends(get_db), user=Depends(interactive_user)):
    rows = db.execute(select(Space, Membership.role).join(Membership, Membership.space_id == Space.id)
                      .where(Membership.user_id == user.id, Membership.active.is_(True))).all()
    return [{**public_row(space), "my_role": role} for space, role in rows]


@router.post("", status_code=201)
def create_space(data: s.NameInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    from .capacity import account_provision
    settings = request.app.state.settings
    account_provision(db, user.id, settings.default_quota_bytes, settings, organization=True)
    space = Space(name=data.name, kind="organization", owner_id=user.id, quota_bytes=settings.default_quota_bytes)
    db.add(space)
    db.flush()
    db.add(Membership(space_id=space.id, user_id=user.id, role="admin"))
    audit(db, user, "space.create", space.id, space.id)
    db.commit()
    return public_row(space)


@router.get("/{space_id}/members")
def members(space_id: str, db=Depends(get_db), user=Depends(interactive_user)):
    require(member(db, user, space_id))
    rows = db.execute(select(Membership, User).join(User, Membership.user_id == User.id)
                      .where(Membership.space_id == space_id)).all()
    return [{**public_row(m), "name": u.name, "email": u.email, 'username': u.username} for m, u in rows]


@router.post("/{space_id}/members")
def save_member(space_id: str, data: s.MemberInput, db=Depends(get_db), user=Depends(interactive_user)):
    space = fetch(db, Space, space_id, lock=True)
    require(space.kind == "organization" and space_admin(db, user, space_id))
    target = db.scalar(select(User).where(or_(User.email == data.email.lower(), User.username == data.email.lower()), User.active.is_(True)))
    require(target is not None, "请先创建或邀请该账号", 404)
    require(target.id != space.owner_id or (data.active and data.role == "admin"), "不可移除空间所有者", 409)
    m = db.scalar(select(Membership).where(Membership.user_id == target.id, Membership.space_id == space_id))
    if not m:
        m = Membership(user_id=target.id, space_id=space_id)
        db.add(m)
    m.role, m.active = data.role, data.active
    audit(db, user, "space.membership", target.id, space_id, role=data.role, active=data.active)
    notify(db, target.id, "组织成员权限已更新", space.name)
    db.commit()
    return public_row(m)


@router.get("/{space_id}/projects")
def projects(space_id: str, db=Depends(get_db), user=Depends(interactive_user)):
    require(member(db, user, space_id))
    return [public_row(p) for p in db.scalars(select(Project).where(Project.space_id == space_id))]


@router.post("/{space_id}/projects", status_code=201)
def create_project(space_id: str, data: s.NameInput, db=Depends(get_db), user=Depends(interactive_user)):
    require(space_admin(db, user, space_id))
    p = Project(space_id=space_id, name=data.name)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, can_apply=True))
    audit(db, user, "project.create", p.id, space_id)
    db.commit()
    return public_row(p)


@router.get("/{space_id}/projects/{project_id}/members")
def project_members(space_id: str, project_id: str, db=Depends(get_db), user=Depends(interactive_user)):
    require(member(db, user, space_id))
    project = fetch(db, Project, project_id)
    require(project.space_id == space_id, "项目不存在", 404)
    return [public_row(x) for x in db.scalars(select(ProjectMember).where(ProjectMember.project_id == project_id))]


@router.post("/{space_id}/projects/{project_id}/members")
def save_project_member(space_id: str, project_id: str, data: s.ProjectMemberInput,
                        db=Depends(get_db), user=Depends(interactive_user)):
    require(space_admin(db, user, space_id))
    project = fetch(db, Project, project_id, lock=True)
    require(project.space_id == space_id, "项目不存在", 404)
    target = fetch(db, User, data.user_id)
    require(member(db, target, space_id), "用户不在组织中", 400)
    pm = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id,
                                               ProjectMember.user_id == target.id))
    if not pm:
        pm = ProjectMember(project_id=project_id, user_id=target.id)
        db.add(pm)
    pm.active, pm.can_apply = data.active, data.can_apply
    audit(db, user, "project.membership", project_id, space_id, user_id=target.id, active=data.active)
    db.commit()
    return public_row(pm)
