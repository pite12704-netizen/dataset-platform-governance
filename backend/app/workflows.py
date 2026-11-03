"""【学习标注：发布、申请与授权】实现两条独立状态机。

发布：草稿 → 提交 → 审核 → 发布/退回。
访问：申请 → 数据负责人审批 → Grant → 到期/撤销；许可确认 Acceptance 单独记录。
"""
import hashlib
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import select, and_, or_, func

from . import schemas as s
from .common import audit, fetch, notify, public_row, require
from .db import get_db, now
from .models import (Acceptance, Application, Asset, Dataset, Grant, ImageReviewBatch, Publication, Upload, User, Version,
                     Membership, Project, ProjectMember)
from .policy import (can_view_version, discover, manage_dataset, project_member, review_publication)
from .security import interactive_user

router = APIRouter(tags=["发布审核与申请授权"])


def owner_scope(user):
    return and_(Dataset.owner_id==user.id, select(Membership.id).where(Membership.space_id==Dataset.space_id,
        Membership.user_id==user.id,Membership.active.is_(True)).exists())


def project_scope(user, project_column):
    return select(ProjectMember.id).join(Project,ProjectMember.project_id==Project.id).join(
        Membership,and_(Membership.space_id==Project.space_id,Membership.user_id==user.id,Membership.active.is_(True))).where(
        ProjectMember.project_id==project_column,ProjectMember.user_id==user.id,ProjectMember.active.is_(True)).exists()


def listing_result(items,has_more,paged,offset,limit):
    return {'items':items,'has_more':has_more,'next_offset':offset+limit if has_more else None} if paged else items


def require_quality_gate(db, version, assets, settings=None):
    from .quality_contract import annotation_snapshot, effective_required, publication_policy,blind_report_valid
    if effective_required(version):
        from .review_templates import for_version
        require(for_version(version) is None,'自定义目标尚未接入独立认证评测，不能套用人物服饰认证门槛',409)
        from .models import GovernanceReport
        from .governance import manifest
        reports = db.scalars(select(GovernanceReport).where(GovernanceReport.version_id == version.id,
            GovernanceReport.kind == 'evaluate', GovernanceReport.status == 'approved')).all()
        snapshot = annotation_snapshot(db,version.id,complete=True)['hash']
        policy = publication_policy(db,version,settings) if settings else None
        require(any(r.summary.get('quality_gate', {}).get('passed') and r.reviewer_id and blind_report_valid(db,r) and
                    r.summary.get('evaluation_target')=='approved_annotations' and
                    (not policy or not policy.get('require_blind_evaluation') or bool(r.summary.get('evaluation_set_id'))) and
                    r.summary.get('input_manifest') == manifest(assets) and
                    r.summary.get('version_annotation_manifest')==snapshot and
                    (policy is None or r.summary.get('publication_policy')==policy) for r in reports),
                '此版本要求属性质量验收，尚无通过独立审核的有效评测报告', 409)


def require_vision_coverage(db, version, assets, batches):
    from .models import ImageReviewImage, Job
    from .vision import image_assets
    if batches or db.scalar(select(Job.id).where(Job.version_id == version.id, Job.kind == 'vision').limit(1)):
        covered = set(db.scalars(select(ImageReviewImage.asset_id).where(
            ImageReviewImage.batch_id.in_([b.id for b in batches]), ImageReviewImage.status == 'approved')))
        require({a.id for a in image_assets(assets)} <= covered,
                '存在未识别或未完整复核的图片，请完成此版本所有视觉分片', 409)


def locked_version(db, version_id):
    # Dataset metadata determines the reviewer and the audience. Freeze it before
    # locking the version, using the same order as dataset/version edits.
    dataset_id = db.scalar(select(Version.dataset_id).where(Version.id == version_id))
    require(dataset_id is not None, "记录不存在", 404)
    ds = fetch(db, Dataset, dataset_id, lock=True)
    return fetch(db, Version, version_id, lock=True), ds


@router.post("/versions/{version_id}/submit")
def submit(version_id: str, request:Request, db=Depends(get_db), user=Depends(interactive_user)):
    version, ds = locked_version(db, version_id)
    require(manage_dataset(db, user, ds) and ds.status == "active")
    require(version.status in {"draft", "rejected"}, "版本不能重复提交", 409)
    from .quality_contract import require_tier
    require_tier(db,version)
    from .imports import require_complete
    require_complete(db, version.id)
    require(not db.scalar(select(Upload.id).where(Upload.version_id == version.id, Upload.status.in_({'uploading','finalizing','aborting'}))),
            "请完成或取消所有上传", 409)
    assets = db.scalars(select(Asset).where(Asset.version_id == version.id)).all()
    require(assets and all(a.status == "clean" for a in assets), "所有文件必须通过检查后才能提交", 409)
    batches = db.scalars(select(ImageReviewBatch).where(ImageReviewBatch.version_id == version.id,
                        ImageReviewBatch.status != "discarded")).all()
    require(not batches or all(batch.status == "completed" for batch in batches),
            "人物识别批次必须完成人工复核后才能提交发布", 409)
    require_vision_coverage(db, version, assets, batches)
    from .models import ImageReviewImage
    require(not batches or not db.scalar(select(ImageReviewImage.id).where(
        ImageReviewImage.batch_id.in_([b.id for b in batches]), ImageReviewImage.status != 'approved')),
        '整图完整性尚未独立确认，不能发布为完整审核集', 409)
    version.status = "pending_review"
    require_quality_gate(db, version, assets, request.app.state.settings)
    from .quality_contract import require_delivery_evidence
    require_delivery_evidence(db,version,assets)
    publication = Publication(version_id=version.id, submitter_id=user.id)
    db.add(publication)
    audit(db, user, "publication.submit", version.id, ds.space_id)
    db.commit()
    return public_row(publication)


@router.get("/publications")
def publications(db=Depends(get_db), user=Depends(interactive_user),limit:int=Query(50,ge=1,le=100),
                 offset:int=Query(0,ge=0,le=100000),paged:bool=False):
    staff=select(Membership.id).where(Membership.space_id==Dataset.space_id,Membership.user_id==user.id,
        Membership.active.is_(True),Membership.role=='admin').exists()
    reviewer=or_(and_(Dataset.visibility=='public',bool(user.platform_admin)),and_(Dataset.visibility!='public',staff))
    rows=db.execute(select(Publication,Version,Dataset,reviewer.label('reviewer')).join(Version,Publication.version_id==Version.id)
        .join(Dataset,Version.dataset_id==Dataset.id).where(or_(Publication.submitter_id==user.id,reviewer))
        .order_by(Publication.created_at.desc(),Publication.id.desc()).offset(offset).limit(limit+1)).all()
    result = []
    identifiers=[v.id for _,v,_,_ in rows[:limit]]
    counts=dict(db.execute(select(Asset.version_id,func.count()).where(Asset.version_id.in_(identifiers)).group_by(Asset.version_id)).all())
    ranked=select(Asset.id,func.row_number().over(partition_by=Asset.version_id,order_by=Asset.id).label('position')).where(Asset.version_id.in_(identifiers)).subquery()
    assets_by_version={}
    for asset in db.scalars(select(Asset).join(ranked,ranked.c.id==Asset.id).where(ranked.c.position<=20).order_by(Asset.id)):
        assets_by_version.setdefault(asset.version_id,[]).append(asset)
    for p,v,ds,is_reviewer in rows[:limit]:
        assets=assets_by_version.get(v.id,[])
        result.append({**public_row(p),'dataset_name':ds.name,'version_number':v.number,'license':v.license,
            'source':v.source,'card':v.card,'quality_tier':v.quality_tier,
            'assets':[public_row(a) for a in assets],'asset_count':counts.get(v.id,0),
            'assets_truncated':counts.get(v.id,0)>len(assets),
            'can_review':p.submitter_id!=user.id and ds.owner_id!=user.id and bool(is_reviewer)})
    return listing_result(result,len(rows)>limit,paged,offset,limit)


@router.post("/publications/{publication_id}/decision")
def publication_decision(publication_id: str, data: s.Decision, request: Request,
                         db=Depends(get_db), user=Depends(interactive_user)):
    version_id = db.scalar(select(Publication.version_id).where(Publication.id == publication_id))
    require(version_id is not None, "记录不存在", 404)
    version, ds = locked_version(db, version_id)
    publication = fetch(db, Publication, publication_id, lock=True)
    require(review_publication(db, user, ds), "发布须由管理员审批：组织内发布需要所属组织管理员，公开上架需要平台管理员")
    require(publication.submitter_id != user.id and ds.owner_id != user.id, "不能审核自己的发布申请")
    if ds.visibility == "public" and request.app.state.settings.require_admin_mfa:
        require(request.state.mfa, "发布审核需要管理员 MFA")
    require(publication.status == "pending" and version.status == "pending_review", "申请已处理", 409)
    require(ds.status == "active", "数据集已下架或封禁", 409)
    if data.approve:
        from .quality_contract import require_tier, require_delivery_evidence
        from .imports import require_complete
        require_complete(db, version.id)
        assets = db.scalars(select(Asset).where(Asset.version_id == version.id)).all()
        require(assets and all(a.status == "clean" for a in assets), "文件检查未全部通过", 409)
        batches = db.scalars(select(ImageReviewBatch).where(ImageReviewBatch.version_id == version.id,
                            ImageReviewBatch.status != "discarded")).all()
        require(all(batch.status == "completed" for batch in batches),
                "人物识别批次必须完成人工复核后才能批准发布", 409)
        require_vision_coverage(db, version, assets, batches)
        from .models import ImageReviewImage
        require(not batches or not db.scalar(select(ImageReviewImage.id).where(
            ImageReviewImage.batch_id.in_([b.id for b in batches]), ImageReviewImage.status != 'approved')),
            '整图完整性尚未独立确认', 409)
        manifest = [{"id": a.id, "sha256": a.sha256, "size": a.size, "name": a.filename} for a in assets]
        from .delivery import current_release
        from .vision import image_assets
        build=current_release(db,version,assets) if batches and image_assets(assets) else None
        if build:
            from .models import Job
            build=fetch(db,Job,build.id,lock=True)
            require(build.status=='completed' and build.artifact_bytes>0,'交付产物已回收，请重新构建',409)
        # Lock order matches task completion: Dataset -> Version -> Job -> Space.
        require_tier(db,version)
        require_quality_gate(db, version, assets, request.app.state.settings)
        require_delivery_evidence(db,version,assets)
        if build:
            build.artifact_expires_at=None
            version.card={**version.card,'_release_build_id':build.id,'_release_sha256':build.result['sha256']}
        version.manifest_hash = hashlib.sha256(json.dumps(sorted(manifest, key=lambda a: a["id"]),
                                          sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if build:
            from .delivery import release_receipt
            version.card={**version.card,'_release_receipt':release_receipt(version,build)}
        version.status, version.published_at = "published", now()
        publication.status = "approved"
    else:
        require(data.comment, "退回时请填写原因", 400)
        version.status, publication.status = "rejected", "rejected"
    publication.reviewer_id, publication.comment, publication.decided_at = user.id, data.comment, now()
    audit(db, user, "publication.decision", version.id, ds.space_id, approved=data.approve)
    notify(db, publication.submitter_id, "数据集发布审核已处理", f"{ds.name}：{publication.status} {data.comment}")
    db.commit()
    return public_row(publication)


@router.post("/applications", status_code=201)
def apply(data: s.ApplicationInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    application = create_application(db, user, data, request.app.state.settings)
    db.commit()
    return public_row(application)


def create_application(db, user, data, settings):
    version = fetch(db, Version, data.version_id)
    ds = fetch(db, Dataset, version.dataset_id)
    require(discover(db, user, ds, settings), "数据集不可见", 404)
    require(version.status == "published" and ds.status == "active", "该版本不接受申请", 409)
    require(ds.access_mode == "application", "开放数据无需申请", 400)
    if data.project_id:
        require(project_member(db, user, data.project_id, apply=True), "没有代表该项目申请的权限")
    old = db.scalar(select(Application).where(Application.applicant_id == user.id,
                    Application.idempotency_key == data.idempotency_key))
    if old:
        require(old.version_id == data.version_id and old.project_id == data.project_id
                and old.purpose == data.purpose and old.duration_days == data.duration_days,
                "幂等键已用于其他申请", 409)
        return old
    require(ds.owner_id != user.id, "数据负责人无需申请自己的数据", 400)
    application = Application(applicant_id=user.id, **data.model_dump())
    db.add(application)
    db.flush()
    audit(db, user, "application.submit", application.id, ds.space_id, version_id=version.id)
    notify(db, ds.owner_id, "收到数据集使用申请", ds.name)
    return application


@router.get("/applications")
def applications(db=Depends(get_db), user=Depends(interactive_user),limit:int=Query(50,ge=1,le=100),
                 offset:int=Query(0,ge=0,le=100000),paged:bool=False):
    owner=owner_scope(user)
    records=db.execute(select(Application,Version.number,Dataset.name,User.name,owner.label('owner'))
        .join(Version,Application.version_id==Version.id).join(Dataset,Version.dataset_id==Dataset.id)
        .join(User,Application.applicant_id==User.id)
        .where(or_(Application.applicant_id==user.id,owner,project_scope(user,Application.project_id)))
        .order_by(Application.created_at.desc(),Application.id.desc()).offset(offset).limit(limit+1)).all()
    result=[{**public_row(app),'dataset_name':name,'version_number':number,'can_review':bool(is_owner),
             'applicant_name':applicant} for app,number,name,applicant,is_owner in records[:limit]]
    return listing_result(result,len(records)>limit,paged,offset,limit)


@router.post("/applications/{application_id}/decision")
def application_decision(application_id: str, data: s.Decision, db=Depends(get_db), user=Depends(interactive_user)):
    version_id = db.scalar(select(Application.version_id).where(Application.id == application_id))
    require(version_id is not None, "记录不存在", 404)
    version, ds = locked_version(db, version_id)
    app = fetch(db, Application, application_id, lock=True)
    require(manage_dataset(db, user, ds), "仅数据负责人可以处理使用申请")
    require(app.status == "pending", "申请已处理", 409)
    require(ds.status == "active" and version.status == "published", "数据集已停止授权", 409)
    applicant = fetch(db, User, app.applicant_id)
    require(applicant.active, "申请账号已停用", 409)
    if app.project_id:
        require(project_member(db, applicant, app.project_id, apply=True), "申请人已失去项目资格", 409)
    app.status = "approved" if data.approve else "rejected"
    app.comment, app.reviewer_id, app.decided_at = data.comment, user.id, now()
    if data.approve:
        db.add(Grant(application_id=app.id, version_id=version.id,
                     user_id=None if app.project_id else app.applicant_id,
                     project_id=app.project_id, expires_at=now() + timedelta(days=app.duration_days)))
    else:
        require(data.comment, "拒绝时请填写原因", 400)
    audit(db, user, "application.decision", app.id, ds.space_id, approved=data.approve)
    notify(db, app.applicant_id, "数据使用申请已处理", f"{ds.name}：{app.status} {data.comment}")
    db.commit()
    return public_row(app)


@router.get("/grants")
def grants(db=Depends(get_db), user=Depends(interactive_user),limit:int=Query(50,ge=1,le=100),
           offset:int=Query(0,ge=0,le=100000),paged:bool=False):
    owner=owner_scope(user)
    records=db.execute(select(Grant,Dataset.name,owner.label('owner')).join(Version,Grant.version_id==Version.id)
        .join(Dataset,Version.dataset_id==Dataset.id).where(or_(Grant.user_id==user.id,owner,project_scope(user,Grant.project_id)))
        .order_by(Grant.created_at.desc(),Grant.id.desc()).offset(offset).limit(limit+1)).all()
    result=[{**public_row(grant),'dataset_name':name,'can_revoke':bool(is_owner),
             'effective':grant.revoked_at is None and grant.expires_at>now()} for grant,name,is_owner in records[:limit]]
    return listing_result(result,len(records)>limit,paged,offset,limit)


@router.post("/grants/{grant_id}/revoke")
def revoke(grant_id: str, data: s.ReportInput, db=Depends(get_db), user=Depends(interactive_user)):
    grant = fetch(db, Grant, grant_id, lock=True)
    ds = db.get(Dataset, db.get(Version, grant.version_id).dataset_id)
    require(manage_dataset(db, user, ds))
    if not grant.revoked_at:
        grant.revoked_at = now()
        audit(db, user, "grant.revoke", grant.id, ds.space_id, reason=data.reason)
        notify(db, db.get(Application, grant.application_id).applicant_id, "数据访问授权已撤销", data.reason)
        db.commit()
    return public_row(grant)


@router.post("/versions/{version_id}/accept-license")
def accept_license(version_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    v = fetch(db, Version, version_id)
    require(can_view_version(db, user, v, request.app.state.settings), "版本不可见", 404)
    require(v.status == "published", "版本尚未发布", 409)
    old = db.scalar(select(Acceptance).where(Acceptance.user_id == user.id, Acceptance.version_id == v.id))
    if not old:
        db.add(Acceptance(user_id=user.id, version_id=v.id, license=v.license))
        audit(db, user, "license.accept", v.id, db.get(Dataset, v.dataset_id).space_id)
        db.commit()
    return {"message": "已确认使用许可"}
