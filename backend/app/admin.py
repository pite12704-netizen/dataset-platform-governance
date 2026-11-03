"""【学习标注：治理后台】通知、真实统计、审计、用户、身份映射、下架封禁、举报和配额。"""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import case, func, select, update

from . import schemas as s
from .auth import add_user, challenge, replace_password
from .common import audit, fetch, notify, page, public_row, require
from .db import get_db
from .models import (Application, Asset, Audit, Dataset, Grant, Identity, Job, LoginSession, MailOutbox, Membership,
                     Notification, Report, Space, User, Version)
from .policy import member, space_admin
from .security import admin_user, interactive_user

router = APIRouter(tags=["管理与运营"])


class Prewarm(s.Input):
    version_id:str=Field(min_length=32,max_length=32)
    limit:int=Field(default=100,ge=1,le=500)
    force:bool=False


@router.post('/admin/thumbnails/prewarm')
def prewarm(data:Prewarm,request:Request,db=Depends(get_db),user=Depends(admin_user)):
    from .models import Thumbnail
    from .tasks import asset_manifest,dispatch
    from .vision import image_assets
    from .db import uid
    version=fetch(db,Version,data.version_id);dataset=fetch(db,Dataset,version.dataset_id,lock=True)
    owner=fetch(db,User,dataset.owner_id)
    require(owner.active and owner.verified and not owner.must_change_password and dataset.status=='active','数据负责人或数据集不可用',409)
    candidate=select(Asset).where(Asset.version_id==version.id,Asset.status=='clean')
    if not data.force:candidate=candidate.where(~select(Thumbnail.id).where(Thumbnail.asset_id==Asset.id,Thumbnail.source_sha256==Asset.sha256).exists())
    assets=image_assets(db.scalars(candidate.order_by(Asset.id).limit(data.limit+1)).all())
    created=[]
    for asset in assets[:data.limit]:
        pending=db.scalar(select(Job.id).where(Job.kind=='thumbnail',Job.version_id==version.id,
            Job.status.in_({'queued','running'}),Job.parameters['asset_id'].as_string()==asset.id).limit(1))
        if pending:continue
        job=Job(space_id=dataset.space_id,user_id=owner.id,version_id=version.id,kind='thumbnail',
            parameters={'asset_id':asset.id,'input_assets':asset_manifest([asset])},idempotency_key='prewarm:'+uid())
        db.add(job);db.flush();created.append(job.id)
    audit(db,user,'thumbnail.prewarm',version.id,dataset.space_id,jobs=len(created));db.commit()
    for identifier in created:dispatch(request.app,identifier)
    return {'job_ids':created,'has_more':len(assets)>data.limit}


@router.post('/admin/mail/probe')
def mail_probe(data:s.EmailInput,request:Request,db=Depends(get_db),user=Depends(admin_user)):
    import secrets
    from datetime import timedelta
    from .db import now
    from .models import Challenge
    from .security import token_hash
    cfg=request.app.state.settings
    require(cfg.mail_delivery_mode!='disabled','尚未配置邮件服务',503)
    fetch(db,User,user.id,lock=True)
    recent=db.scalar(select(Challenge.id).where(Challenge.user_id==user.id,Challenge.kind=='mail_probe',
        Challenge.created_at>now()-timedelta(minutes=1)).limit(1))
    require(not recent,'请稍后再发送测试邮件',429)
    token=secrets.token_urlsafe(32)
    mail=MailOutbox(recipient=data.email,subject='数据集平台：邮件收件探针',
        body=f'请以发起测试的管理员账号登录后确认收件：\n{cfg.frontend_url}/?mail_probe={token}\n链接30分钟有效；收到不代表所有邮箱均能可靠送达。')
    db.add(mail);db.flush()
    db.add(Challenge(user_id=user.id,kind='mail_probe',token_hash=token_hash(token),expires_at=now()+timedelta(minutes=30),
        payload={'outbox_id':mail.id,'received_at':None}))
    audit(db,user,'mail.probe.request',mail.id);db.commit()
    if cfg.task_mode=='inline':
        from .auth import deliver_mail
        deliver_mail(db,cfg)
    return {'message':'测试邮件已登记；SMTP 接受与真实收件分开记录'}


@router.get('/admin/mail/probe')
def mail_probe_status(db=Depends(get_db),user=Depends(admin_user)):
    from .models import Challenge
    item=db.scalar(select(Challenge).where(Challenge.user_id==user.id,Challenge.kind=='mail_probe').order_by(Challenge.created_at.desc()).limit(1))
    if not item:return {'configured_test':False}
    mail=db.get(MailOutbox,item.payload['outbox_id'])
    return {'configured_test':True,'requested_at':item.created_at,'smtp_accepted_at':mail.sent_at if mail.delivery_mode=='smtp' else None,
        'transport':mail.delivery_mode,'received_confirmed_at':item.payload.get('received_at'),'attempts':mail.attempts,'last_error':mail.last_error}


@router.get("/admin/model-status")
def model_status(request: Request, user=Depends(admin_user)):
    """运维可观测状态；不能把历史成功/配置存在当成模型现在可达。"""
    from .model_gateway import status
    from .vision import model_settings
    settings = request.app.state.settings
    return {'agent': status(settings) if settings.model_url and settings.model_name else {'configured': False},
            'vision': status(model_settings(settings)) if model_settings(settings).model_url and model_settings(settings).model_name
                      else {'configured': False},
            'vision_enabled': settings.vision_enabled,
            'vision_revision_verified': bool(settings.vision_model_revision.strip())}


@router.get("/notifications")
def notifications(db=Depends(get_db), user=Depends(interactive_user)):
    return [public_row(n) for n in db.scalars(select(Notification).where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc()).limit(100))]


@router.get("/admin/runtime-status")
def runtime_status(request: Request, user=Depends(admin_user)):
    """进程级快照：不暴露连接串、存储凭据或其他进程的推测指标。"""
    import os
    settings = request.app.state.settings
    pool = request.app.state.db.engine.pool
    limiter = getattr(request.app.state, "thread_limiter", None)
    async_db = getattr(request.app.state, "async_db", None)
    return {"process_id": os.getpid(), "scope": "current_api_process",
            "threads": {"limit": settings.api_thread_tokens,
                        "borrowed": limiter.borrowed_tokens if limiter is not None else None},
            "database": {"pool_size": settings.database_pool_size,
                         "max_overflow": settings.database_max_overflow,
                         "wait_timeout_seconds": settings.database_pool_timeout,
                         "checked_out": pool.checkedout() if hasattr(pool, "checkedout") else None},
            "async_database": {"enabled": async_db is not None, "pool_size": settings.database_async_pool_size,
                "max_overflow": settings.database_async_max_overflow,
                "checked_out": async_db.engine.pool.checkedout() if async_db is not None and hasattr(async_db.engine.pool, 'checkedout') else None},
            "storage": {"max_pool_connections": settings.s3_max_pool_connections,
                "downloads_active": request.app.state.download_limiter.borrowed_tokens,
                "downloads_limit": settings.download_max_inflight,
                "uploads_active": request.app.state.upload_limiter.borrowed_tokens,
                "uploads_limit": settings.upload_max_inflight,
                "media_active": request.app.state.media_limiter.borrowed_tokens,
                "media_limit": settings.media_max_inflight,
                "mcp_active": request.app.state.mcp_limiter.borrowed_tokens,
                "mcp_limit": settings.mcp_max_inflight},
            "model": {"max_inflight": settings.model_max_inflight}}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, db=Depends(get_db), user=Depends(interactive_user)):
    item = fetch(db, Notification, notification_id)
    require(item.user_id == user.id, "通知不存在", 404)
    item.read = True
    db.commit()
    return {"message": "已读"}


@router.get("/stats")
def statistics(space_id: str | None = None, db=Depends(get_db), user=Depends(interactive_user)):
    if space_id:
        require(space_admin(db, user, space_id), "统计需要空间管理权限")
        space_ids = select(Space.id).where(Space.id == space_id)
    else:
        space_ids = select(Membership.space_id).where(Membership.user_id == user.id,
                         Membership.active.is_(True), Membership.role == "admin")
    # 聚合在数据库执行：不把全空间的资产、版本、申请和任务实例加载到 Python。
    dataset_ids = select(Dataset.id).where(Dataset.space_id.in_(space_ids))
    version_ids = select(Version.id).where(Version.dataset_id.in_(dataset_ids))
    versions, published = db.execute(select(func.count(), func.sum(case((Version.status == "published", 1), else_=0)))
                                    .select_from(Version).where(Version.dataset_id.in_(dataset_ids))).one()
    applications, pending, approved = db.execute(select(func.count(),
        func.sum(case((Application.status == "pending", 1), else_=0)),
        func.sum(case((Application.status == "approved", 1), else_=0)))
        .select_from(Application).where(Application.version_id.in_(version_ids))).one()
    jobs, failed = db.execute(select(func.count(), func.sum(case((Job.status == "failed", 1), else_=0)))
                             .select_from(Job).where(Job.space_id.in_(space_ids))).one()
    return {"datasets": db.scalar(select(func.count()).select_from(Dataset).where(Dataset.space_id.in_(space_ids))),
            "versions": versions, "published": published or 0,
            "files": db.scalar(select(func.count()).select_from(Asset).where(Asset.version_id.in_(version_ids))),
            "storage_bytes": db.scalar(select(func.sum(Space.used_bytes)).where(Space.id.in_(space_ids))) or 0,
            "applications": applications, "pending_applications": pending or 0,
            "approval_rate": round((approved or 0) / applications * 100, 1) if applications else 0,
            "jobs": jobs, "failed_jobs": failed or 0,
            "downloads": db.scalar(select(func.count()).select_from(Audit).where(Audit.space_id.in_(space_ids),
                                               Audit.action.in_(["asset.download", "package.download"]))) or 0}


@router.get("/audits")
def audits(space_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
           db=Depends(get_db), user=Depends(interactive_user)):
    if space_id:
        require(space_admin(db, user, space_id))
        query = select(Audit).where(Audit.space_id == space_id)
    else:
        query = select(Audit).where(Audit.actor_id == user.id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(Audit.created_at.desc()).offset(offset).limit(limit))
    return {"items": [public_row(a) for a in items], "total": total}


@router.get("/admin/users")
def users(db=Depends(get_db), user=Depends(admin_user)):
    return [public_row(u) for u in db.scalars(select(User).order_by(User.created_at.desc()))]


class Invite(s.Input):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    name: str = Field(min_length=1, max_length=100)


@router.post("/admin/users/invite", status_code=201)
def invite(data: Invite, request: Request, db=Depends(get_db), user=Depends(admin_user)):
    import secrets
    require(request.app.state.settings.mail_delivery_mode != 'disabled', '邮件邀请未开通，请创建本地账号', 503)
    target = add_user(db, s.Register(email=data.email, name=data.name, password=secrets.token_urlsafe(32)),
                      request.app.state.settings, verified=True)
    challenge(db, target, "reset", request.app.state.settings)
    audit(db, user, "user.invite", target.id)
    db.commit()
    if request.app.state.settings.task_mode == "inline":
        from .auth import deliver_mail
        deliver_mail(db, request.app.state.settings)
    return public_row(target)


@router.post('/admin/users/local', status_code=201)
def create_local(data: s.Register, request: Request, db=Depends(get_db), user=Depends(admin_user)):
    require(request.app.state.settings.deployment == 'private', '本地账号创建仅用于私有部署', 403)
    require(data.username, '本地账号必须填写登录账号', 422)
    target = add_user(db, data, request.app.state.settings, verified=True)
    target.must_change_password = True
    audit(db, user, 'user.local_create', target.id); db.commit()
    return public_row(target)


class AdminPassword(s.Input):
    password: str = Field(min_length=12, max_length=128)
    reason: str = Field(min_length=5, max_length=500)


@router.post('/admin/users/{user_id}/reset-password')
def admin_password_reset(user_id: str, data: AdminPassword, db=Depends(get_db), user=Depends(admin_user)):
    target = fetch(db, User, user_id, lock=True)
    require(target.id != user.id, '修改自己的密码请前往账号安全', 409)
    require(target.active, '请先恢复账号状态', 409)
    replace_password(db, target, data.password, force_change=True)
    audit(db, user, 'user.admin_password_reset', target.id, reason=data.reason); db.commit()
    return {'message': '初始密码已重置，原会话与凭据已撤销；用户下次登录必须修改密码，MFA 保持有效'}


@router.get('/admin/mail/status')
def mail_status(request: Request, db=Depends(get_db), user=Depends(admin_user)):
    pending = db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.sent_at.is_(None)))
    failed = db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.sent_at.is_(None), MailOutbox.attempts >= 5))
    return {'delivery_mode': request.app.state.settings.mail_delivery_mode, 'pending': pending, 'retry_exhausted': failed,
            'submitted_to_smtp': db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.delivery_mode == 'smtp', MailOutbox.sent_at.is_not(None))),
            'spooled': db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.delivery_mode == 'spool', MailOutbox.sent_at.is_not(None))),
            'note': 'SMTP 接受不等于收件箱送达；不返回邮件地址、正文或重置链接'}


class UserStatus(s.Input):
    active: bool


@router.put("/admin/users/{user_id}")
def user_status(user_id: str, data: UserStatus, db=Depends(get_db), user=Depends(admin_user)):
    target = fetch(db, User, user_id, lock=True)
    require(target.id != user.id, "不能停用当前管理员", 409)
    target.active = data.active
    if not data.active:
        from .auth import revoke_account_credentials
        revoke_account_credentials(db,target)
    audit(db, user, "user.status", target.id, active=data.active)
    db.commit()
    return public_row(target)


class IdentityInput(s.Input):
    user_id: str
    subject: str = Field(min_length=1, max_length=255)
    provider: str = "oidc"


@router.post("/admin/identities")
def identity(data: IdentityInput, request: Request, db=Depends(get_db), user=Depends(admin_user)):
    target = fetch(db, User, data.user_id)
    settings = request.app.state.settings
    issuer = settings.mcp_issuer if data.provider == "mcp" else settings.oidc_issuer
    require(issuer, "身份服务尚未配置", 400)
    db.add(Identity(user_id=target.id, issuer=issuer, subject=data.subject))
    audit(db, user, "identity.link", target.id)
    db.commit()
    return {"message": "已关联企业身份"}


@router.get("/admin/datasets")
def admin_datasets(db=Depends(get_db), user=Depends(admin_user)):
    # Governance metadata only; platform admins do not gain content-download rights.
    return [public_row(d) for d in db.scalars(select(Dataset).order_by(Dataset.created_at.desc()))]


@router.post("/admin/datasets/{dataset_id}/status")
def dataset_status(dataset_id: str, data: s.StatusInput, db=Depends(get_db), user=Depends(admin_user)):
    ds = fetch(db, Dataset, dataset_id, lock=True)
    ds.status = data.status
    audit(db, user, "dataset.status", ds.id, ds.space_id, status=data.status, reason=data.reason)
    notify(db, ds.owner_id, "数据集状态已变更", f"{ds.name}：{data.status}，{data.reason}")
    db.commit()
    return public_row(ds)


@router.get("/admin/reports")
def reports(db=Depends(get_db), user=Depends(admin_user)):
    return [public_row(r) for r in db.scalars(select(Report).order_by(Report.created_at.desc()))]


@router.post("/admin/reports/{report_id}/resolve")
def resolve_report(report_id: str, data: s.ReportInput, db=Depends(get_db), user=Depends(admin_user)):
    report = fetch(db, Report, report_id, lock=True)
    report.status, report.resolution = "resolved", data.reason
    audit(db, user, "report.resolve", report.id, resolution=data.reason)
    notify(db, report.user_id, "举报已处理", data.reason)
    db.commit()
    return public_row(report)


class Quota(s.Input):
    quota_bytes: int = Field(gt=0)


@router.get("/admin/spaces")
def spaces(db=Depends(get_db), user=Depends(admin_user)):
    return [public_row(x) for x in db.scalars(select(Space))]


@router.put("/admin/spaces/{space_id}/quota")
def quota(space_id: str, data: Quota, request: Request, db=Depends(get_db), user=Depends(admin_user)):
    from .capacity import account_provision
    original=fetch(db,Space,space_id)
    fetch(db,User,original.owner_id,lock=True)
    original=fetch(db,Space,space_id,lock=True)
    account_provision(db,original.owner_id,data.quota_bytes-original.quota_bytes,request.app.state.settings)
    space = fetch(db, Space, space_id, lock=True)
    require(data.quota_bytes >= space.used_bytes + space.reserved_bytes, "配额不得低于已使用及预留容量", 409)
    space.quota_bytes = data.quota_bytes
    audit(db, user, "space.quota", space.id, space.id, quota_bytes=data.quota_bytes)
    db.commit()
    return public_row(space)


class Transfer(s.Input):
    owner_id: str
    reason: str = Field(min_length=5,max_length=2000)
    current_password: str = Field(min_length=1,max_length=128)


@router.post("/spaces/{space_id}/datasets/{dataset_id}/transfer")
def transfer(space_id: str, dataset_id: str, data: Transfer, request:Request, db=Depends(get_db), user=Depends(interactive_user)):
    ds = fetch(db, Dataset, dataset_id)
    require(ds.space_id == space_id, "数据集不在该空间", 404)
    # Compatibility route delegates to the same authorization and quiescence
    # checks; it must never remain an organization-admin content-access bypass.
    from .ownership import Transfer as OwnershipTransfer,transfer_owner
    return transfer_owner(dataset_id,OwnershipTransfer(target_user_id=data.owner_id,
        reason=data.reason,current_password=data.current_password),request,db,user)
