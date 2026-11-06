"""【学习标注：后台任务】质量检查、完整转换和数据打包的创建、调度、执行与恢复。

接口层：create_job/queue_job；调度层：dispatch；执行层：run_job。
租约与 run_token 防止任务重启后重复提交结果，heartbeat 会重新检查授权和取消状态。
"""
import hashlib
import logging
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from celery import Celery
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import defer
from types import SimpleNamespace
from sqlalchemy import func, or_, select, update

from . import schemas as s
from .checks import inspect_asset
from .common import audit, fetch, notify, public_row, require
from .config import get_settings
from .db import Database, get_db, now, uid
from .models import Asset, Dataset, ImageReviewBatch, Job, QualityReport, Space, Upload, User, Version
from .policy import can_read_content, manage_dataset
from .security import current_user, enforce_scope, interactive_user
from .storage import make_storage
from .job_payloads import hydrate_job,AUTH,HOT_BYTES,canonical as payload_canonical

router = APIRouter(prefix="/jobs", tags=["后台任务"])
logger = logging.getLogger(__name__)


def authorized_job(db, user, job, settings):
    if job.kind in {'evaluate','package'}:hydrate_job(db,job,fields=('parameters',))
    v = db.get(Version, job.version_id)
    blind=job.parameters.get('governance_options',{}).get('evaluation_set_id') if job.kind=='evaluate' else None
    if blind:
        try:
            from .blind_evaluation import require_evaluator
            require_evaluator(db,user,blind,v.id,mutable=job.status in {'queued','running'})
            return user.id==job.user_id
        except Exception:return False
    if job.kind in {'vision','vision_plan','convert','curate','dedup','split','evaluate','release','thumbnail'}:
        ds = db.get(Dataset, v.dataset_id)
        if ds.status != "active" or not manage_dataset(db, user, ds):
            return False
    authorized=bool(user and user.id == job.user_id and can_read_content(db, user, v, settings))
    if authorized and job.kind=='package' and not manage_dataset(db,user,db.get(Dataset,v.dataset_id)):
        from .evidence_access import truth_condition
        query=select(Asset.id).where(Asset.version_id==v.id,truth_condition())
        if job.parameters.get('input_assets') is not None:
            query=query.where(Asset.id.in_([a['id'] for a in job.parameters['input_assets']]))
        authorized=not bool(db.scalar(query.limit(1)))
    return authorized


@router.post("", status_code=201)
def create_job(data: s.JobInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    job = queue_job(db, user, data, request.app.state.settings)
    db.commit()
    dispatch(request.app, job.id)
    db.refresh(job)
    return public_row(job)


def validate_job(db, user, data, settings):
    version = fetch(db, Version, data.version_id)
    ds = fetch(db, Dataset, version.dataset_id)
    if data.kind=='evaluate' and data.evaluation_set_id:
        from .blind_evaluation import require_evaluator
        require_evaluator(db,user,data.evaluation_set_id,version.id)
    else:require(can_read_content(db, user, version, settings), "缺少数据内容访问权限；请先取得授权并确认使用许可")
    require(ds.status == "active", "数据集不可加工", 409)
    asset_query=select(Asset).where(Asset.version_id==version.id)
    if not manage_dataset(db,user,ds):
        from .evidence_access import truth_condition
        asset_query=asset_query.where(~truth_condition())
    assets=db.scalars(asset_query).all()
    require(assets and all(a.status == "clean" for a in assets), "文件检查未通过", 409)
    require(not data.asset_ids or data.kind == 'vision', '仅视觉任务接受受控资产清单', 422)
    if data.kind == "vision":
        from .vision import configured, image_assets
        configured(settings)
        require(manage_dataset(db, user, ds), "首期由数据负责人发起人物识别")
        require(version.status in {"draft", "rejected"}, "版本已冻结，不能新增识别候选", 409)
        if data.asset_ids:
            selected = set(data.asset_ids)
            require(len(selected) == len(data.asset_ids) and selected <= {a.id for a in image_assets(assets)},
                    '识别清单包含重复、跨版本或非图片资产', 422)
            assets = [a for a in assets if a.id in selected]
        images = image_assets(assets)
        require(0 < len(images) <= settings.vision_max_images, "图像数量超出当前批次限制", 413)
        require(all(a.size <= 20 * 1024**2 for a in images), "人物识别每张图片最多 20MB", 413)
        active=db.scalar(select(func.count()).select_from(Job).where(Job.space_id==ds.space_id,Job.kind=='vision',
            Job.status.in_({'queued','running'})))
        replay=db.scalar(select(Job.id).where(Job.user_id==user.id,Job.idempotency_key==data.idempotency_key))
        require(replay or active<settings.space_max_active_vision_shards,'组织视觉队列已满，请等待已有任务完成',429)
    if data.kind in {'dedup', 'split', 'evaluate', 'curate'}:
        from .governance import validate
        validate(db, user, version, assets, data, settings)
    if data.kind == 'release':
        from .delivery import validate_release
        validate_release(db, user, version, assets, settings)
    if data.kind == "convert":
        require(manage_dataset(db, user, ds), "首期仅数据负责人可以创建派生版本")
        require(data.target_format is not None, "请选择目标格式", 400)
        require(all(a.size <= settings.max_conversion_bytes for a in assets),
                "转换支持每文件最多 64MB，原文件将完整保留", 413)
        target = data.target_format
        require(all((Path(a.filename).suffix.lower() in {".csv", ".json"} and target in {"csv", "json"}) or
                    (Path(a.filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and
                     target in {"png", "jpg", "webp"}) for a in assets), "不支持此输入与目标格式组合", 400)
    return version, ds, assets


def queue_job(db, user, data, settings):
    version = fetch(db, Version, data.version_id)
    fetch(db, Dataset, version.dataset_id, lock=True)
    db.scalar(select(Version).where(Version.id == version.id)
              .execution_options(populate_existing=True).with_for_update())
    if data.kind=='vision':fetch(db,Space,fetch(db,Dataset,version.dataset_id).space_id,lock=True)
    version, ds, assets = validate_job(db, user, data, settings)
    old = db.scalar(select(Job).where(Job.user_id == user.id, Job.idempotency_key == data.idempotency_key))
    if old:
        hydrate_job(db,old)
        require(old.version_id == version.id and old.kind == data.kind and
                old.parameters.get("target_format") == data.target_format and
                old.parameters.get('requested_asset_ids', []) == sorted(data.asset_ids), "幂等键已用于其他任务", 409)
        if data.kind in {'dedup', 'split', 'evaluate', 'curate'}:
            from .governance import options
            frozen = {'annotation_manifest','version_annotation_manifest','publication_policy','_max_candidates','_max_comparisons'}
            require({k:v for k,v in old.parameters.get('governance_options',{}).items() if k not in frozen} == options(data), '幂等键已用于其他治理参数', 409)
        if data.kind == 'release':
            require(old.parameters.get('delivery_format') == data.delivery_format and
                    old.parameters.get('selection',{})==data.selection.model_dump(exclude_none=True), '幂等键已用于另一交付格式或筛选条件', 409)
        return old
    job = Job(space_id=ds.space_id, user_id=user.id, version_id=version.id, kind=data.kind,
              parameters={"target_format": data.target_format, "input_assets": asset_manifest(assets),
                          'requested_asset_ids': sorted(data.asset_ids)},
              idempotency_key=data.idempotency_key)
    if data.kind == "vision":
        from .vision import fingerprint, model_settings
        from .review_templates import for_version
        template=for_version(version)
        job.parameters = {**job.parameters, "model": model_settings(settings).model_name,
                          "pipeline_fingerprint": fingerprint(settings,template)}
        if template:job.parameters={**job.parameters,'review_template':template}
    if data.kind in {'dedup', 'split', 'evaluate', 'curate'}:
        from .governance import options
        job.parameters = {**job.parameters, 'governance_options': options(data)}
        if data.kind=='dedup':
            job.parameters['governance_options']['_max_candidates']=settings.dedup_max_candidates
            job.parameters['governance_options']['_max_comparisons']=settings.dedup_max_comparisons
        if data.kind=='evaluate' and data.evaluation_target=='approved_annotations':
            from .governance import annotation_manifest
            from .quality_contract import annotation_snapshot, publication_policy
            job.parameters['governance_options']['annotation_manifest']=annotation_manifest(db,data.batch_id)
            job.parameters['governance_options']['version_annotation_manifest']=annotation_snapshot(db,version.id,complete=True)['hash']
            job.parameters['governance_options']['publication_policy']=publication_policy(db,version,settings)
    if data.kind == 'release':
        from .delivery import release_contract, release_reservation
        job.parameters = {**job.parameters, 'delivery_format': data.delivery_format,
                          'selection':data.selection.model_dump(exclude_none=True),
                          'release_contract': release_contract(db, version, assets,data.selection.model_dump(exclude_none=True))}
        require(job.parameters['release_contract']['selected_asset_ids'],'筛选没有命中已审核图片',422)
        from .export_formats import validate as validate_format
        validate_format(job.parameters['release_contract'],data.delivery_format)
        reserve_package(db, job, assets, amount=release_reservation(db, version), settings=settings)
    if data.kind == "package":
        reserve_package(db, job, assets, settings=settings)
    if data.kind == 'curate':
        reserve_package(db, job, assets, settings=settings)
    db.add(job)
    db.flush()
    audit(db, user, "job.create", job.id, ds.space_id, kind=data.kind)
    return job


@router.post('/vision-batches', status_code=201)
def queue_vision_batches(data: s.JobInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    """一个版本按冻结的资产清单分片；重复点击不重复安排已存在的任务。"""
    require(data.kind == 'vision' and not data.asset_ids, '此入口自动划分视觉任务', 422)
    version = fetch(db, Version, data.version_id)
    dataset = fetch(db, Dataset, version.dataset_id, lock=True)
    fetch(db, Version, version.id, lock=True)
    require(manage_dataset(db, user, dataset) and dataset.status == 'active')
    require(version.status in {'draft', 'rejected'}, '版本已冻结', 409)
    from .imports import require_complete
    require_complete(db, version.id)
    from .vision import configured, fingerprint, image_assets
    from .review_templates import for_version
    configured(request.app.state.settings)
    assets = db.scalars(select(Asset).where(Asset.version_id == version.id).order_by(Asset.id)).all()
    require(assets and all(a.status == 'clean' for a in assets), '文件检查未通过', 409)
    assigned = set()
    for job in db.scalars(select(Job).where(Job.version_id == version.id, Job.kind == 'vision')):
        hydrate_job(db,job,fields=('parameters',))
        if job.status in {'failed', 'cancelled'} and job.attempts >= 3:
            continue
        batch = db.scalar(select(ImageReviewBatch).where(ImageReviewBatch.job_id == job.id))
        if not batch or batch.status != 'discarded':
            assigned.update(a['id'] for a in job.parameters.get('input_assets', []))
    remaining = [a.id for a in image_assets(assets) if a.id not in assigned]
    if not remaining:return {'job_ids':[],'scheduled_images':0,'remaining_images':0,'automatic':True}
    from .quality_contract import media_manifest
    fetch(db,Space,dataset.space_id,lock=True)
    existing=db.scalar(select(Job).where(Job.version_id==version.id,Job.kind=='vision_plan',Job.status.in_({'queued','running'})))
    if existing:
        hydrate_job(db,existing,fields=('parameters',))
        return {'job_ids':[],'scheduled_images':existing.parameters['cursor'],
                'remaining_images':len(existing.parameters['asset_ids'])-existing.parameters['cursor'],'plan_id':existing.id,'automatic':True}
    active_plans=db.scalar(select(func.count()).select_from(Job).where(Job.space_id==dataset.space_id,Job.kind=='vision_plan',
        Job.status.in_({'queued','running'})))
    require(active_plans<request.app.state.settings.space_max_vision_plans,'组织已有多个视觉计划运行，请等待或取消',429)
    plan=Job(space_id=dataset.space_id,user_id=user.id,version_id=version.id,kind='vision_plan',status='running',
        parameters={'asset_ids':remaining,'cursor':0,'children':[],'shard_size':request.app.state.settings.vision_max_images,
                    'pipeline_fingerprint':fingerprint(request.app.state.settings,for_version(version)),'population_manifest':media_manifest(assets)},
        idempotency_key='vision-plan:'+uid())
    db.add(plan);db.flush();plan_id=plan.id
    audit(db,user,'vision_plan.create',plan.id,dataset.space_id,images=len(remaining))
    db.commit()
    from .vision_plans import advance
    created=advance(plan_id,request.app.state.db,request.app.state.settings)
    for identifier in created:
        dispatch(request.app, identifier)
    if request.app.state.settings.task_mode=='inline':
        while followups:=advance(plan_id,request.app.state.db,request.app.state.settings):
            for identifier in followups:dispatch(request.app,identifier)
    return {'job_ids':created,'scheduled_images':min(len(remaining),len(created)*request.app.state.settings.vision_max_images),
            'remaining_images':max(0,len(remaining)-len(created)*request.app.state.settings.vision_max_images),'plan_id':plan_id,'automatic':True}


@router.get("")
def jobs(request: Request, compact: bool = False, limit: int = Query(50,ge=1,le=200), offset: int = Query(0,ge=0),
         version_id: str | None = Query(None,max_length=32), governance_only: bool = False,
         db=Depends(get_db), user=Depends(current_user)):
    enforce_scope(request, "jobs:read")
    filters=[Job.user_id==user.id]
    if version_id:filters.append(Job.version_id==version_id)
    if governance_only:filters.append(Job.kind.in_(['dedup','split','curate','evaluate']))
    if compact:
        # 列表不读取全量文件清单、逐文件报告或模型检查点；仅展开详情时读取。
        fields=['id','space_id','user_id','version_id','kind','status','progress','attempts','created_at','finished_at','error']
        result_fields=['batch_id','version_id','report_id','files','total','checked','passed','failed','duplicate_files',
                       'images','persons','method','processed_files','total_files','phase','size',
                       'integrity','malware','coverage','sensitive','sensitive_method','malware_method','media_type']
        columns=[getattr(Job,name) for name in fields]
        columns += [func.coalesce(Job.parameters[AUTH].as_string(),
            Job.parameters['governance_options']['evaluation_set_id'].as_string()).label('_evaluation_set_id')]
        columns += [Job.result[name].label('_result_'+name) for name in result_fields]
        candidates=db.execute(select(*columns).where(*filters)
            .order_by(Job.created_at.desc(),Job.id.desc()).offset(offset).limit(limit+1)).mappings().all()
        rows=[];permissions={}
        for row in candidates[:limit]:
            brief={name:row[name] for name in fields}
            params={'governance_options':{'evaluation_set_id':row['_evaluation_set_id']}} if row['_evaluation_set_id'] else {}
            probe=SimpleNamespace(**brief,parameters=params)
            permission_key=(probe.version_id,probe.kind,row['_evaluation_set_id'],probe.status in {'queued','running'})
            if probe.kind=='package':
                # 非负责人打包还需逐任务核对受限真值选择，不能复用版本权限缓存。
                allowed=authorized_job(db,user,fetch(db,Job,probe.id),request.app.state.settings)
            else:
                if permission_key not in permissions:
                    permissions[permission_key]=authorized_job(db,user,probe,request.app.state.settings)
                allowed=permissions[permission_key]
            if not allowed:continue
            brief['error']=brief['error'][:2000] if brief['error'] else None
            brief['result']={name:row['_result_'+name][:2000] if isinstance(row['_result_'+name],str) else row['_result_'+name]
                for name in result_fields if row['_result_'+name] is not None and isinstance(row['_result_'+name],(str,int,float,bool))}
            brief['details_deferred']=True;rows.append(brief)
        has_more=len(candidates)>limit
        return {'items':rows,'has_more':has_more,'next_offset':offset+limit if has_more else None}
    return [public_row(j) for j in db.scalars(select(Job).where(*filters)
            .order_by(Job.created_at.desc()).limit(200)) if authorized_job(db, user, j, request.app.state.settings)]


@router.get("/{job_id}")
def job_detail(job_id: str, request: Request, db=Depends(get_db), user=Depends(current_user)):
    enforce_scope(request, "jobs:read")
    job = fetch(db, Job, job_id)
    require(authorized_job(db, user, job, request.app.state.settings), "任务不存在或授权已失效", 404)
    return public_row(job)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    job = locked_job(db, job_id)
    require(job.user_id == user.id, "任务不存在", 404)
    require(job.status in {"queued", "running", "cancelled"} or job.kind=='vision_plan' and job.status=='failed', "任务已结束", 409)
    release_reservation(db, job)
    if job.kind=='vision_plan':
        children=db.scalars(select(Job).where(Job.id.in_(job.parameters.get('children',[])),Job.status.in_({'queued','running'})).with_for_update()).all()
        for child in children:child.status,child.finished_at='cancelled',now()
    job.status, job.finished_at = "cancelled", now()
    audit(db, user, "job.cancel", job.id, job.space_id)
    db.commit()
    return public_row(job)


@router.post("/{job_id}/retry")
def retry_job(job_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    job = locked_job(db, job_id)
    require(authorized_job(db, user, job, request.app.state.settings))
    require(job.status in {"failed", "cancelled"} and job.attempts < 3, "任务不可重试或已达到三次尝试上限", 409)
    job.status, job.error, job.progress = "queued", None, 0
    if job.kind == "package":
        reserve_package(db, job, input_assets(db, job), settings=request.app.state.settings)
    elif job.kind=='release':
        from .delivery import release_reservation
        reserve_package(db,job,input_assets(db,job),amount=release_reservation(db,fetch(db,Version,job.version_id)),settings=request.app.state.settings)
    remember_attempt(job)
    job.lease_until, job.run_token, job.finished_at = None, None, None
    job.next_dispatch_at, job.dispatch_token, job.dispatch_attempts = None, None, 0
    db.commit()
    dispatch(request.app, job.id)
    db.refresh(job)
    return public_row(job)


def prepare_package_download(db, job_id, request, record=True):
    from .security import optional_user
    user = current_user(optional_user(request, db))
    enforce_scope(request, "content:read")
    brief=db.execute(select(Job.kind,Job.version_id,Job.user_id).where(Job.id==job_id)).one_or_none()
    if brief and brief[0]=='release':
        version=fetch(db,Version,brief[1])
        if version.status in {'published','withdrawn'} and '_release_receipt' in version.card:
            dataset=fetch(db,Dataset,version.dataset_id)
            require(brief[2]==user.id and dataset.status=='active' and manage_dataset(db,user,dataset))
            from .delivery import published_release,download_descriptor
            pinned=published_release(db,version)
            require(pinned and pinned.id==job_id,'此构建不是当前批准交付，请使用版本交付入口',409)
            return download_descriptor(db,version.id,request,record)
    job = fetch(db, Job, job_id)
    require(authorized_job(db, user, job, request.app.state.settings))
    require(job.kind in {"package","release"} and job.status == "completed", "打包尚未完成", 409)
    if job.kind=='release':
        from .delivery import release_contract,frozen_release_valid
        version=fetch(db,Version,job.version_id)
        valid=frozen_release_valid(db,version,input_assets(db,job),job) if version.status=='published' else job.result.get('contract')==release_contract(db,version,input_assets(db,job),job.parameters.get('selection'))
        require(job.artifact_bytes and valid,
                '审核快照已变化，请重新构建交付',409)
    else:
        require(job.artifact_expires_at and job.artifact_expires_at > now(), "打包产物已过期，请重新创建任务", 410)
    from .downloads import byte_range
    size = job.result["size"]
    start, end, partial = byte_range(request.headers.get("range"), size)
    if record:
        audit(db, user, "package.download", job.id, job.space_id)
        db.commit()
    headers = {"Content-Disposition": f'attachment; filename="dataset-{job.version_id}.zip"',
               "Content-Length": str(end - start + 1), "Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return {'key': f"packages/{job.id}/{job.result['artifact_id']}", 'start': start, 'end': end,
            'headers': headers, 'status_code': 206 if partial else 200, 'media_type': 'application/zip'}


@router.get("/{job_id}/download")
async def package_download(job_id: str, request: Request):
    from .db import run_db
    from .async_storage import download_response
    descriptor = await run_db(request, prepare_package_download, job_id, request)
    return await download_response(request, descriptor, lambda db: prepare_package_download(db, job_id, request, False))


def job_queue(kind):
    """路由只取 MySQL 中的任务类型；客户端不能指定 Celery 队列或任务名。"""
    return 'platform.vision' if kind == 'vision' else 'celery'


def dispatch(app, job_id):
    settings = app.state.settings
    if settings.task_mode == "inline":
        run_job(job_id, app.state.db, app.state.storage, settings)
    elif settings.task_mode == "celery":
        try:
            from .dispatching import send
            send(job_id, app.state.db, settings, celery_app(settings))
        except Exception:
            # The durable queued row is the outbox; beat dispatches it after broker recovery.
            logger.warning("Broker unavailable; durable task remains queued: %s", job_id)


class Stopped(Exception):
    pass


def reserve_package(db, job, assets, amount=None, *, settings):
    amount = amount if amount is not None else sum(a.size for a in assets) + len(assets) * 2048 + 4096
    from .capacity import growth
    growth(db, amount, settings)
    changed = db.execute(update(Space).where(Space.id == job.space_id,
        Space.used_bytes + Space.reserved_bytes + amount <= Space.quota_bytes)
        .values(reserved_bytes=Space.reserved_bytes + amount))
    require(changed.rowcount == 1, "空间剩余配额不足以生成打包产物", 409)
    job.reserved_bytes = amount


def release_reservation(db, job):
    if job.reserved_bytes:
        db.execute(update(Space).where(Space.id == job.space_id)
                   .values(reserved_bytes=Space.reserved_bytes - job.reserved_bytes))
        job.reserved_bytes = 0


def asset_manifest(assets):
    return [{"id": a.id, "size": a.size, "sha256": a.sha256} for a in assets]


def input_assets(db, job):
    hydrate_job(db,job,fields=('parameters',))
    if job.kind == "scan":
        asset = fetch(db, Asset, job.parameters["asset_id"])
        require(asset.version_id == job.version_id and asset.status != 'removing', "任务输入已变化", 409)
        return [asset]
    manifest = job.parameters.get("input_assets")
    if manifest is None:  # Compatibility: freeze legacy jobs before any processing.
        assets = db.scalars(select(Asset).where(Asset.version_id == job.version_id)).all()
        require(assets and all(a.status == "clean" for a in assets), "文件检查未通过", 409)
        job.parameters = {**job.parameters, "input_assets": asset_manifest(assets)}
        return assets
    # 大版本用有界 IN 查询取代逐图片 SELECT，仍按冻结清单顺序逐项核对。
    identifiers=[item['id'] for item in manifest]
    found={}
    for offset in range(0,len(identifiers),500):
        for asset in db.scalars(select(Asset).where(Asset.id.in_(identifiers[offset:offset+500]))):
            found[asset.id]=asset
    assets = [found.get(identifier) for identifier in identifiers]
    require(assets and all(a and a.version_id == job.version_id and a.status == "clean"
            and a.size == item["size"] and a.sha256 == item["sha256"]
            for a, item in zip(assets, manifest)), "任务输入已变化或文件检查未通过", 409)
    return assets


def locked_job(db, job_id):
    job = fetch(db, Job, job_id)
    version = fetch(db, Version, job.version_id)
    fetch(db, Dataset, version.dataset_id, lock=True)
    db.scalar(select(Version).where(Version.id == version.id)
              .execution_options(populate_existing=True).with_for_update())
    current=db.scalar(select(Job).where(Job.id == job_id)
                     .execution_options(populate_existing=True).with_for_update())
    return hydrate_job(db,current)


def remember_attempt(job):
    if job.run_token and job.kind in {"package", "release", "convert", "curate",'thumbnail'}:
        tokens = set(job.parameters.get("cleanup_attempts", []))
        tokens.add(job.run_token)
        job.parameters = {**job.parameters, "cleanup_attempts": sorted(tokens)}


def cleanup_attempt(storage, job, token):
    # Only server-generated attempt tokens for this job are eligible; never enumerate
    # the business bucket or touch another attempt's accepted result.
    if job.status == "completed" and job.run_token == token:
        return
    if job.kind=='thumbnail':
        from .thumbnails import PROFILES
        for profile in PROFILES:storage.delete(f'thumbnails/{job.parameters["asset_id"]}/{token}/{profile}')
    elif job.kind in {"package", "release"}:
        storage.delete(f"packages/{job.id}/{token}")
    elif job.kind in {"convert", "curate"}:
        for asset in job.parameters.get("input_assets", []):
            storage.delete(f"derived/{job.id}/{token}/{asset['id']}")


def run_job(job_id, database, storage, settings):
    with database.Session() as plan_db:
        kind=plan_db.scalar(select(Job.kind).where(Job.id==job_id))
    if kind=='vision_plan':
        from .vision_plans import advance
        identifiers=advance(job_id,database,settings)
        for identifier in identifiers:
            if settings.task_mode=='inline':run_job(identifier,database,storage,settings)
            elif settings.task_mode=='celery':
                try:
                    from .dispatching import send
                    send(identifier,database,settings,celery_app(settings))
                except Exception:logger.warning('Durable vision shard dispatch deferred: %s',identifier)
        return
    run_token = uid()
    with database.Session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.attempts >= 3 or not (job.status == "queued" or
                (job.status == "running" and job.lease_until and job.lease_until < now())):
            return
        remember_attempt(job)
        job.status, job.run_token, job.attempts = "running", run_token, job.attempts + 1
        job.dispatch_attempts, job.dispatch_token, job.next_dispatch_at = 0, None, None
        job.lease_until, job.error = now() + timedelta(minutes=5), None
        db.commit()
    last_heartbeat = 0
    pending_progress,pending_details=None,None

    def heartbeat(force=False, progress=None, details=None):
        nonlocal last_heartbeat,pending_progress,pending_details
        if progress is not None:pending_progress=progress
        if details is not None:pending_details=details
        if not force and time.monotonic() - last_heartbeat < 2:
            return
        with database.Session() as check:
            options=[defer(Job.result)]
            if kind not in {'evaluate','package'}:options.append(defer(Job.parameters))
            job = check.scalar(select(Job).where(Job.id==job_id).options(*options))
            if job is None:raise Stopped('任务已移除')
            user = check.get(User, job.user_id)
            if job.status != "running" or job.run_token != run_token or not authorized_job(check, user, job, settings):
                raise Stopped("任务取消或数据授权失效")
            values = {"lease_until": now() + timedelta(minutes=5)}
            if pending_progress is not None:
                values["progress"] = max(0, min(99, int(pending_progress)))
            if pending_details is not None:
                require(isinstance(pending_details,dict) and len(payload_canonical(pending_details))<=HOT_BYTES,
                        '任务进度载荷超过热状态容量',413)
                values['result']=pending_details
            check.execute(update(Job).where(Job.id == job_id, Job.run_token == run_token, Job.status == "running")
                          .values(**values))
            check.commit()
        last_heartbeat = time.monotonic()

    converted = []
    committed = False
    try:
        heartbeat(True,progress=1)
        with database.Session() as db:
            job = locked_job(db, job_id)
            if job.status != "running" or job.run_token != run_token:
                raise Stopped("任务已取消")
            version = db.get(Version, job.version_id)
            assets = input_assets(db, job)
            if job.kind == "vision":
                require(version.status in {"draft", "rejected"}, "版本已冻结，不能新增识别候选", 409)
            db.commit()
            db.expunge_all()
        for token in job.parameters.get("cleanup_attempts", []):
            cleanup_attempt(storage, job, token)
        result = {}
        scanned = {}
        if job.kind == "scan":
            asset = next(a for a in assets if a.id == job.parameters["asset_id"])
            scanned[asset.id] = inspect_asset(asset, storage, settings, heartbeat)
            result = scanned[asset.id]
        elif job.kind=='thumbnail':
            from .thumbnails import build
            thumbnail_rows=build(assets[0],storage,run_token,heartbeat)
            result={'profiles':[row['profile'] for row in thumbnail_rows]}
        elif job.kind == "quality":
            total=len(assets)
            heartbeat(True,progress=5,details={'phase':'checking','processed_files':0,'total_files':total})
            for index,asset in enumerate(assets):
                heartbeat(progress=5+index*90//max(1,total),
                    details={'phase':'checking','processed_files':index,'total_files':total})
                scanned[asset.id] = inspect_asset(asset, storage, settings, heartbeat)
                heartbeat(force=index+1==total,progress=5+(index+1)*90//max(1,total),
                    details={'phase':'checking','processed_files':index+1,'total_files':total})
            hashes = [a.sha256 for a in assets]
            result = {"method": "rules-v1", "model": None, "total": len(assets),
                      "checked": sum(x.get("coverage", 0) for x in scanned.values()),
                      "passed": sum(x.get("status") == "clean" for x in scanned.values()),
                      "failed": sum(x.get("status") == "rejected" for x in scanned.values()),
                      "duplicate_files": len(hashes) - len(set(hashes)),
                      "not_checked": ["语义评分", "二进制内容敏感信息识别"],
                      "assets": scanned}
        elif job.kind == "vision":
            from .vision import analyze, fingerprint, model_settings
            from .models import VisionCheckpoint
            template=job.parameters.get('review_template')
            from .review_templates import for_version
            with database.Session() as template_db:
                require(template==for_version(fetch(template_db,Version,job.version_id)),'版本审核模板已变化',409)
            signature = fingerprint(settings,template)
            require(model_settings(settings).model_name == job.parameters['model'] and
                    job.parameters.get('pipeline_fingerprint', signature) == signature,
                    '模型或处理配置已变更，请创建新任务', 409)

            def load_checkpoint(asset):
                heartbeat(True)
                with database.Session() as checkpoint_db:
                    row = checkpoint_db.scalar(select(VisionCheckpoint).where(
                        VisionCheckpoint.job_id == job_id, VisionCheckpoint.asset_id == asset.id))
                    if row:
                        require(row.fingerprint == signature, '检查点配置不一致', 409)
                        return row.result

            def save_checkpoint(asset, value):
                with database.Session() as checkpoint_db:
                    current = checkpoint_db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                    require(current.status == 'running' and current.run_token == run_token, '旧执行不能提交检查点', 409)
                    if not authorized_job(checkpoint_db,checkpoint_db.get(User,current.user_id),current,settings):
                        raise Stopped('任务授权已撤销')
                    checkpoint_db.add(VisionCheckpoint(job_id=job_id, asset_id=asset.id,
                                                       fingerprint=signature, result=value))
                    checkpoint_db.commit()
            vision_rows, provenance = analyze(assets, storage, settings, heartbeat, load_checkpoint, save_checkpoint,
                **({'review_template':template} if template else {}))
            result = {"persons": len(vision_rows), "images": provenance["images"], "human_reviewed": 0}
        elif job.kind == "convert":
            from converter_core import ConvertConfig, convert
            target = job.parameters["target_format"]
            for asset in assets:
                heartbeat(True)
                source = Path(asset.filename).suffix.lower().lstrip(".")
                require((source in {"csv", "json"} and target in {"csv", "json"}) or
                        (source in {"png", "jpg", "jpeg", "webp"} and target in {"png", "jpg", "webp"}),
                        "首期验证的转换范围为 CSV/JSON 和 PNG/JPEG/WebP", 400)
                require(asset.size <= settings.max_conversion_bytes, "超出转换大小限制", 413)
                raw = b"".join(storage.chunks(asset.object_key))
                output = convert(raw, source, target, ConvertConfig())
                if not output.success or not output.content:
                    raise ValueError("转换引擎未返回完整结果")
                heartbeat(True)
                key = f"derived/{job.id}/{run_token}/{asset.id}"
                storage.put_bytes(key, output.content)
                converted.append({"filename": Path(asset.filename).stem + "." + target, "object_key": key,
                                  "size": len(output.content), "sha256": hashlib.sha256(output.content).hexdigest(),
                                  "preview_allowed": False})
            result = {"files": len(converted), "operation": "convert", "target_format": target}
        elif job.kind == 'curate':
            from .governance import retained_images
            with database.Session() as report_db:
                retained = retained_images(report_db, assets, job.parameters['governance_options'])
            for asset in retained:
                heartbeat(True)
                key=f'derived/{job.id}/{run_token}/{asset.id}'
                with tempfile.TemporaryFile() as output:
                    digest=hashlib.sha256();size=0
                    for chunk in storage.chunks(asset.object_key):
                        heartbeat();size+=len(chunk);digest.update(chunk);output.write(chunk)
                        require(size<=asset.size,'原图大小校验失败',409)
                    require(size==asset.size and digest.hexdigest()==asset.sha256,'原图完整性校验失败',409)
                    output.seek(0);storage.put_file(key,output,progress=heartbeat)
                converted.append({'filename':asset.filename,'object_key':key,'size':asset.size,
                    'sha256':asset.sha256,'preview_allowed':False})
            result={'files':len(converted),'operation':'curate','retained_source_ids':[a.id for a in retained]}
        elif job.kind in {'dedup', 'split', 'evaluate'}:
            from .governance import duplicates, split_images, evaluate
            parameters = job.parameters['governance_options']
            if job.kind == 'dedup':
                result = duplicates(assets, storage, parameters, heartbeat)
            else:
                with database.Session() as report_db:
                    result = split_images(report_db, assets, parameters) if job.kind == 'split' else evaluate(report_db, assets, storage, parameters,heartbeat)
        elif job.kind == 'release':
            from .delivery import build_release
            with database.Session() as report_db:
                result = build_release(report_db, job, assets, storage, run_token, heartbeat)
        elif job.kind == "package":
            with tempfile.TemporaryFile() as output:
                with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for asset in assets:
                        heartbeat(True)
                        with archive.open(f"{asset.id}/{asset.filename}", "w", force_zip64=True) as entry:
                            for chunk in storage.chunks(asset.object_key):
                                heartbeat()
                                entry.write(chunk)
                size = output.tell()
                output.seek(0)
                heartbeat(True)
                storage.put_file(f"packages/{job.id}/{run_token}", output, progress=heartbeat)
                result = {"size": size, "files": len(assets), "artifact_id": run_token}
        else:
            raise ValueError("Unknown task kind")
        heartbeat(True)
        with database.Session() as db:
            job = locked_job(db, job_id)
            if job.status != "running" or job.run_token != run_token:
                raise Stopped("任务已取消")
            actor = db.get(User, job.user_id)
            require(authorized_job(db, actor, job, settings), "任务授权已失效")
            input_assets(db, job)
            if job.kind == "scan":
                asset = db.get(Asset, job.parameters["asset_id"])
                asset.checks, asset.status = result, result["status"]
                asset.media_type = result.get("media_type", "application/octet-stream")
                from .vision import image_assets
                if settings.thumbnail_persistence_enabled and settings.environment=='production' and asset.status=='clean' and image_assets([asset]):
                    db.add(Job(space_id=job.space_id,user_id=job.user_id,version_id=job.version_id,kind='thumbnail',
                        parameters={'asset_id':asset.id,'input_assets':asset_manifest([asset])},idempotency_key='thumbnail:'+asset.id))
            elif job.kind=='thumbnail':
                from .thumbnails import commit as commit_thumbnails
                previous_thumbnails=commit_thumbnails(db,job,thumbnail_rows,settings)
                tokens={key.split('/')[-2] for key in previous_thumbnails}
                require(all(len(token)==32 and all(c in '0123456789abcdef' for c in token) for token in tokens),'缩略图清理记录无效',409)
                job.parameters={**job.parameters,'thumbnail_cleanup_tokens':sorted(tokens)}
            elif job.kind == "quality":
                db.add(QualityReport(version_id=job.version_id, job_id=job.id, summary=result))
            elif job.kind == "vision":
                require(db.get(Version, job.version_id).status in {"draft", "rejected"},
                        "版本已冻结，不能新增识别候选", 409)
                from .image_reviews import create_batch
                batch = create_batch(db, job.version_id, job.user_id, vision_rows, provenance, job_id=job.id)
                result["batch_id"] = batch.id
            elif job.kind in {'dedup', 'split', 'evaluate'}:
                from .models import GovernanceReport
                if job.kind=='evaluate' and result.get('evaluation_target')=='approved_annotations':
                    from .governance import annotation_manifest
                    require(result['annotation_manifest']==annotation_manifest(db,result['batch_id']),'最终标注在评测期间变化',409)
                    from .quality_contract import annotation_snapshot
                    require(result.get('version_annotation_manifest')==annotation_snapshot(db,job.version_id,complete=True)['hash'],
                            '整版标注在评测期间变化',409)
                report = GovernanceReport(job_id=job.id, version_id=job.version_id, kind=job.kind, summary=result)
                db.add(report)
                db.flush()
                result = {'report_id': report.id, 'kind': job.kind}
            elif job.kind in {"convert", "curate"}:
                source = db.get(Version, job.version_id)
                ds = db.scalar(select(Dataset).where(Dataset.id == source.dataset_id).with_for_update())
                from .capacity import lock, growth
                lock(db, 'storage-growth')
                space = db.scalar(select(Space).where(Space.id == ds.space_id).with_for_update())
                size = sum(a["size"] for a in converted)
                release_reservation(db, job)
                growth(db, size, settings)
                require(space.used_bytes + space.reserved_bytes + size <= space.quota_bytes, "派生产物超出存储配额", 409)
                space.used_bytes += size
                number = db.scalar(select(func.max(Version.number)).where(Version.dataset_id == ds.id)) + 1
                derived = Version(dataset_id=ds.id, number=number, creator_id=job.user_id,
                                  license=source.license, source=source.source, card={k:v for k,v in source.card.items() if k!='_review_pipeline'},
                                  parent_version_id=source.id, lineage={"job_id": job.id,
                                  "tool": "converter_core" if job.kind=='convert' else 'reviewed_dedup_v1',
                                  "parameters": job.parameters, 'retained_source_ids':result.get('retained_source_ids',[])},
                                  changelog="格式转换生成的派生版本" if job.kind=='convert' else '独立审核去重派生版本，原图完整保留')
                db.add(derived)
                db.flush()
                for item in converted:
                    asset = Asset(version_id=derived.id, **item)
                    db.add(asset)
                    db.flush()
                    db.add(Job(space_id=ds.space_id, user_id=job.user_id, version_id=derived.id, kind="scan",
                               parameters={"asset_id": asset.id}, idempotency_key=f"scan:{asset.id}"))
                result["version_id"] = derived.id
            elif job.kind in {"package", "release"}:
                if job.kind == 'release':
                    from .delivery import release_contract
                    version = db.get(Version, job.version_id)
                    require(version.status in {'draft','rejected'} and
                            job.parameters['release_contract']==release_contract(db,version,input_assets(db,job),job.parameters.get('selection')),
                            '交付构建期间版本或标注已变化，请重新构建',409)
                require(result["size"] <= job.reserved_bytes, "打包产物超出预留容量", 409)
                release_reservation(db, job)
                db.execute(update(Space).where(Space.id == job.space_id)
                           .values(used_bytes=Space.used_bytes + result["size"]))
                job.artifact_bytes = result["size"]
                job.artifact_expires_at = None if job.kind == 'release' else now() + timedelta(hours=24)
                if job.kind=='release':
                    previous=db.scalars(select(Job).where(Job.version_id==job.version_id,Job.kind=='release',
                        Job.id!=job.id,Job.artifact_bytes>0,Job.artifact_expires_at.is_(None))).all()
                    for older in previous:
                        hydrate_job(db,older)
                        if older.result.get('contract',{}).get('annotation_manifest')!=result['contract']['annotation_manifest']:
                            older.artifact_expires_at=now()+timedelta(days=7)
            job.status, job.progress, job.result, job.finished_at = "completed", 100, result, now()
            job.parameters = {k: v for k, v in job.parameters.items() if k != "cleanup_attempts"}
            audit(db, actor, "job.completed", job.id, job.space_id, kind=job.kind)
            notify(db, job.user_id, "后台任务已完成", f"{job.kind} · {job.id[:8]}")
            db.commit()
            committed = True
        if job.kind=='thumbnail':
            for key in previous_thumbnails:
                try:storage.delete(key)
                except Exception:logger.warning('Old thumbnail cleanup deferred')
        if settings.task_mode == "inline" and converted:
            with database.Session() as db:
                followups = db.scalars(select(Job.id).where(Job.version_id == result["version_id"], Job.status == "queued")).all()
            for followup in followups:
                run_job(followup, database, storage, settings)
    except Exception as exc:
        from .diagnostics import report
        report(logger,'job_failed',exc,job_id=job_id)
        with database.Session() as db:
            failed_job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            hydrate_job(db,failed_job)
            # COMMIT can succeed on the server and then lose its acknowledgment.
            # Never delete an artifact referenced by a durable completed result.
            if failed_job.status == "completed" and failed_job.run_token == run_token:
                return
            if failed_job.run_token == run_token:
                release_reservation(db, failed_job)
                remember_attempt(failed_job)
            db.execute(update(Job).where(Job.id == job_id, Job.run_token == run_token, Job.status == "running")
                       .values(status="cancelled" if isinstance(exc, Stopped) else "failed",
                               error=(str(exc.detail) if hasattr(exc, "detail") else type(exc).__name__)[:2000],
                               finished_at=now()))
            db.commit()
            # Flush 已把大字段收缩回引用；事务结束后恢复清理所需清单再离开会话。
            hydrate_job(db,failed_job,fields=('parameters',))
        if not committed:
            try:
                cleanup_attempt(storage, failed_job, run_token)
            except Exception:
                logger.warning("Attempt cleanup deferred: %s", job_id)


_celery = None


def sweep_storage(database, storage):
    """Each cleanup owns its transaction; one missing object must not poison the batch."""
    with database.Session() as db:
        stale = db.scalars(select(Upload.id).where(or_(Upload.status.in_({'uploading','finalizing'}) & (Upload.expires_at < now()),Upload.status=='aborting'))
                           .order_by(Upload.expires_at).limit(200)).all()
        jobs = db.scalars(select(Job.id).where(or_(
            (Job.artifact_bytes > 0) & (Job.artifact_expires_at < now()),
            (Job.status == "running") & (Job.attempts >= 3) & (Job.lease_until < now()),
            Job.status.in_(["failed", "cancelled"]) & Job.parameters["cleanup_attempts"].as_string().is_not(None),
            (Job.kind=='thumbnail') & Job.parameters['thumbnail_cleanup_tokens'].as_string().is_not(None)))
            .order_by(Job.created_at).limit(200)).all()
    for upload_id in stale:
        try:
            with database.Session() as db:
                upload = db.scalar(select(Upload).where(Upload.id == upload_id).with_for_update(skip_locked=True))
                if not upload or upload.status not in {'uploading','finalizing','aborting'} or upload.status!='aborting' and upload.expires_at>=now():
                    continue
                upload.status='aborting';db.commit()
                from .uploads import finish_abort
                finish_abort(db,storage,upload_id)
        except Exception:
            logger.warning("Expired upload cleanup deferred: %s", upload_id)
    for job_id in jobs:
        try:
            with database.Session() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update(skip_locked=True))
                if job is not None:hydrate_job(db,job)
                if not job:
                    continue
                if job.artifact_bytes and job.artifact_expires_at and job.artifact_expires_at < now():
                    storage.delete(f"packages/{job.id}/{job.result['artifact_id']}")
                    db.execute(update(Space).where(Space.id == job.space_id)
                               .values(used_bytes=Space.used_bytes - job.artifact_bytes))
                    job.artifact_bytes = 0
                if job.status == "running" and job.attempts >= 3 and job.lease_until < now():
                    remember_attempt(job)
                    release_reservation(db, job)
                    job.status, job.error, job.finished_at = "failed", "Worker 重试次数已耗尽", now()
                if job.status in {"failed", "cancelled"}:
                    for token in job.parameters.get("cleanup_attempts", []):
                        cleanup_attempt(storage, job, token)
                    job.parameters = {k: v for k, v in job.parameters.items() if k != "cleanup_attempts"}
                if job.kind=='thumbnail':
                    for token in job.parameters.get('thumbnail_cleanup_tokens',[]):cleanup_attempt(storage,job,token)
                    job.parameters={key:value for key,value in job.parameters.items() if key!='thumbnail_cleanup_tokens'}
                db.commit()
        except Exception:
            logger.warning("Job artifact cleanup deferred: %s", job_id)


def celery_app(settings=None):
    global _celery
    if _celery is None:
        settings = settings or get_settings()
        _celery = Celery("dataset", broker=settings.redis_url)
        _celery.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
                            worker_cancel_long_running_tasks_on_connection_loss=True,
                            broker_transport_options={"visibility_timeout": 21600},
                            broker_connection_retry_on_startup=True, task_serializer="json", accept_content=["json"],
                            task_default_queue="celery", task_routes={"dataset.sweep": {"queue": "celery"}},
                            beat_schedule={"durable-outbox": {"task": "dataset.sweep", "schedule": 30.0},
                                "mail-outbox":{"task":"dataset.deliver_mail","schedule":15.0,
                                               "options":{"queue":"platform.mail"}},
                                "retrieval-outbox":{"task":"dataset.retrieval","schedule":10.0,
                                               "options":{"queue":"platform.retrieval", "expires":15}}})

        @_celery.task(name="dataset.retrieval")
        def retrieval_delivery():
            cfg = get_settings()
            if not cfg.retrieval_enabled:
                return
            from .retrieval_indexing import process_batch, reconcile_orphans
            database, storage = Database(cfg.database_url, cfg), None
            try:
                storage = make_storage(cfg)
                # 一次唤醒在有界时间窗内持续消费，避免每10秒只处理8张。
                # 单个事件的HTTP仍有超时；迟到的重复唤醒消息会过期。
                until = time.monotonic()+20
                while time.monotonic() < until:
                    from .retrieval_indexing import eligible as index_eligible
                    from .models import RetrievalEvent
                    with database.Session() as db:
                        if not db.scalar(select(RetrievalEvent.id).where(index_eligible()).limit(1)):
                            break
                    process_batch(database, storage, cfg)
                reconcile_orphans(database, cfg)
            finally:
                if storage is not None:
                    storage.close()
                database.engine.dispose()

        @_celery.task(name='dataset.deliver_mail')
        def mail_delivery():
            cfg=get_settings()
            database=Database(cfg.database_url,cfg)
            try:
                from .auth import deliver_mail
                with database.Session() as db:deliver_mail(db,cfg)
            finally:database.engine.dispose()

        @_celery.task(name="dataset.run_job")
        def execute(job_id):
            cfg = get_settings()
            database = Database(cfg.database_url, cfg)
            storage = None
            try:
                storage = make_storage(cfg)
                run_job(job_id, database, storage, cfg)
            finally:
                try:
                    if storage is not None:
                        storage.close()
                finally:
                    database.engine.dispose()

        @_celery.task(name="dataset.sweep")
        def sweep():
            cfg = get_settings()
            database, storage = Database(cfg.database_url, cfg), None
            try:
                storage = make_storage(cfg)
                with database.Session() as db:
                    from .dispatching import eligible
                    candidates=select(Job.id,Job.kind,Job.created_at,func.row_number().over(
                        partition_by=(Job.space_id,Job.kind),order_by=(Job.created_at,Job.id)).label('position')).where(eligible()).subquery()
                    identifiers=db.execute(select(candidates.c.id,candidates.c.kind).where(candidates.c.position<=20)
                        .order_by(candidates.c.position,candidates.c.created_at,candidates.c.id).limit(200)).all()
                for identifier, kind in identifiers:
                    try:
                        from .dispatching import send
                        send(identifier, database, cfg, _celery)
                    except Exception:
                        logger.warning("Durable task dispatch deferred: %s", identifier)
                sweep_storage(database, storage)
                from .agent_retention import prune
                prune(database,cfg)
            finally:
                try:
                    if storage is not None:
                        storage.close()
                finally:
                    database.engine.dispose()
    return _celery
