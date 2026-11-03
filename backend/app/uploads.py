"""【学习标注：分片上传】建立上传会话、接收分片、完整性校验、完成或取消上传。

关键约束：先预留空间配额；完成时核对大小和 SHA-256；文件先进入隔离区，
随后创建 scan 任务；失败或取消必须释放预留配额。
"""
import asyncio
import hashlib
import logging
import math
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update


from . import schemas as s
from .common import audit, fetch, public_row, require
from .checks import SUPPORTED,require_modality
from .db import get_db, now, uid, run_db
from .models import (Asset, Dataset, ImageReviewBatch, ImageReviewImage, ImageReviewItem,
                     Job, Space, Upload, Version, VisionCheckpoint)
from .policy import manage_dataset
from .security import interactive_user, resolve_interactive_user

router = APIRouter(tags=["分片上传"])
logger = logging.getLogger(__name__)


def editable(db, user, version_id, lock=True):
    version = fetch(db, Version, version_id)
    ds = fetch(db, Dataset, version.dataset_id, lock=lock)
    version = db.scalar(select(Version).where(Version.id == version_id)
                        .execution_options(populate_existing=True).with_for_update()) if lock else version
    require(manage_dataset(db, user, ds) and ds.status == "active")
    require(version.status in {"draft", "rejected"}, "版本已冻结，不能再上传", 409)
    return version, ds


@router.post("/versions/{version_id}/uploads", status_code=201)
def start_upload(version_id: str, data: s.UploadInput, request: Request,
                 db=Depends(get_db), user=Depends(interactive_user)):
    prepared=prepare_storage(version_id,data,request,db,user)
    if 'existing' in prepared:return prepared['existing']
    try:
        user=resolve_interactive_user(request,db)
        return create_upload(version_id,data,request,db,user,storage_session=prepared)
    except Exception:
        db.rollback();raise
    finally:cleanup_prepared(db,request,prepared)


def prepare_storage(version_id,data,request,db,user):
    """Read-only preflight, then no SQL transaction while creating the multipart session."""
    version,ds=editable(db,user,version_id,lock=False)
    require(not version.card.get('_review_pipeline') or Path(data.filename).suffix.lower() not in {'.jpg','.jpeg','.png','.webp'},'联合标注已固定图片范围，请新建版本添加图片',409)
    settings=request.app.state.settings
    require(data.size<=settings.max_file_bytes and data.size<=settings.max_upload_bytes,'超过当前上传或扫描容量',413)
    require(Path(data.filename).suffix.lower().lstrip('.') in SUPPORTED,'首期尚未验证该文件格式',400)
    require_modality(ds.modality,data.filename)
    old=db.scalar(select(Upload).where(Upload.user_id==user.id,Upload.idempotency_key==data.idempotency_key))
    if old:
        require(old.version_id==version_id and old.sha256==data.sha256 and old.size==data.size
            and old.filename==data.filename and old.preview_allowed==data.preview_allowed,'幂等键已用于其他上传',409)
        return {'existing':public_row(old)}
    key=f'assets/{ds.space_id}/{uid()}'
    db.commit();db.expire_all()
    return {'key':key,'upload_id':request.app.state.storage.initiate(key)}


def cleanup_prepared(db,request,prepared):
    if 'key' not in prepared:return
    try:
        persisted=db.scalar(select(Upload.id).where(Upload.object_key==prepared['key']))
        db.commit()
        if not persisted:request.app.state.storage.abort(prepared['key'],prepared['upload_id'])
    except Exception:
        db.rollback();logger.warning('Multipart initialization cleanup deferred')


def create_upload(version_id, data, request, db, user, commit=True, *, storage_session):
    """Short transaction; the caller prepared S3 outside all business locks."""
    version,ds=editable(db,user,version_id)
    require_modality(ds.modality,data.filename)
    require(not version.card.get('_review_pipeline') or Path(data.filename).suffix.lower() not in {'.jpg','.jpeg','.png','.webp'},'联合标注已固定图片范围，请新建版本添加图片',409)
    settings=request.app.state.settings
    require(data.size<=settings.max_file_bytes and data.size<=settings.max_upload_bytes,'超过当前上传或扫描容量',413)
    old=db.scalar(select(Upload).where(Upload.user_id==user.id,Upload.idempotency_key==data.idempotency_key))
    if old:
        require(old.version_id==version_id and old.sha256==data.sha256 and old.size==data.size
            and old.filename==data.filename and old.preview_allowed==data.preview_allowed,'幂等键已用于其他上传',409)
        return public_row(old)
    from .capacity import growth
    growth(db,data.size,settings)
    changed=db.execute(update(Space).where(Space.id==ds.space_id,
        Space.used_bytes+Space.reserved_bytes+data.size<=Space.quota_bytes)
        .values(reserved_bytes=Space.reserved_bytes+data.size))
    require(changed.rowcount==1,'空间存储配额不足',409)
    upload=Upload(version_id=version.id,user_id=user.id,filename=data.filename,size=data.size,sha256=data.sha256,
        idempotency_key=data.idempotency_key,preview_allowed=data.preview_allowed,object_key=storage_session['key'],
        storage_upload_id=storage_session['upload_id'],part_size=settings.part_size_bytes,
        expires_at=now()+timedelta(hours=settings.upload_expiry_hours))
    db.add(upload);db.flush();audit(db,user,'upload.start',upload.id,ds.space_id,size=data.size)
    if commit:db.commit()
    return public_row(upload)


def own_upload(db, user, upload_id, lock=True):
    upload = fetch(db, Upload, upload_id)
    require(upload.user_id == user.id, "上传会话不存在", 404)
    if lock:
        version = fetch(db, Version, upload.version_id)
        fetch(db, Dataset, version.dataset_id, lock=True)
        fetch(db, Version, version.id, lock=True)
        upload = db.scalar(select(Upload).where(Upload.id == upload_id)
                           .execution_options(populate_existing=True).with_for_update())
    return upload


def upload_identity(upload_id: str, request: Request, db=Depends(get_db)):
    return resolve_upload_user(request, db, upload_id)


def resolve_upload_user(request, db, upload_id, lock=True):
    from .models import ImportItem
    if (request.headers.get('authorization', '').startswith('Bearer dp_ing_')
            or db.scalar(select(ImportItem.id).where(ImportItem.upload_id == upload_id))):
        from .imports import upload_actor
        return upload_actor(request, db, upload_id, lock=lock)
    return resolve_interactive_user(request, db)


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str, db=Depends(get_db), user=Depends(upload_identity)):
    return public_row(own_upload(db, user, upload_id, lock=False))


@router.put("/uploads/{upload_id}/parts/{part_number}")
async def upload_part(upload_id: str, part_number: int, request: Request):
    def prepare(db):
        user = resolve_upload_user(request, db, upload_id, lock=False)
        upload = own_upload(db, user, upload_id, lock=False)
        editable(db, user, upload.version_id, lock=False)
        require(upload.status == "uploading" and upload.expires_at > now(), "上传会话已关闭或过期", 409)
        total_parts = math.ceil(upload.size / upload.part_size)
        require(1 <= part_number <= total_parts, "分片序号错误", 400)
        return min(upload.part_size, upload.size - (part_number - 1) * upload.part_size)

    # 会话关闭后才开始接收网络数据，不占用连接池或跨线程传递 ORM 用户。
    expected = await run_db(request, prepare)
    from anyio import WouldBlock
    from fastapi import HTTPException
    limiter, borrower = request.app.state.upload_limiter, object()
    try:
        limiter.acquire_on_behalf_of_nowait(borrower)
    except WouldBlock:
        raise HTTPException(503, "上传并发已满，请稍后重试", headers={"Retry-After": "1"}) from None
    try:
        body = bytearray()
        try:
            async with asyncio.timeout(request.app.state.settings.upload_receive_timeout_seconds):
                async for chunk in request.stream():
                    require(len(body) + len(chunk) <= expected, "分片超出声明大小", 413)
                    body.extend(chunk)
        except TimeoutError:
            require(False, "接收分片超时，请重试", 408)
        require(len(body) == expected, "分片大小不匹配", 400)
        def persist(db):
            # Serialize competing writes to this upload, while allowing other files
            # in the same dataset/version to write to object storage concurrently.
            user = resolve_upload_user(request, db, upload_id, lock=False)
            own_upload(db,user,upload_id,lock=False)
            upload = fetch(db,Upload,upload_id,lock=True)
            editable(db, user, upload.version_id, lock=False)
            require(upload.status == "uploading" and upload.expires_at > now(), "上传会话已关闭或过期", 409)
            etag = request.app.state.storage.put_part(upload.object_key, upload.storage_upload_id, part_number, bytes(body))
            upload.parts = {**upload.parts, str(part_number): {
                "etag": etag, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}}
            db.commit()
            return etag
        etag = await run_db(request, persist)
        def revalidate(db):
            # A fresh transaction observes cancellation, membership or credential
            # revocation during S3 I/O. Stored parts confer no completion rights.
            user=resolve_upload_user(request,db,upload_id)
            upload=own_upload(db,user,upload_id)
            editable(db,user,upload.version_id)
            require(upload.status=='uploading' and upload.expires_at>now(),'上传会话已关闭或过期',409)
        await run_db(request,revalidate)
        return {"part_number": part_number, "etag": etag}
    finally:
        limiter.release_on_behalf_of(borrower)


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, request: Request, db=Depends(get_db), user=Depends(upload_identity)):
    upload=own_upload(db,user,upload_id)
    if upload.status=='completed':return {'asset_id':upload.asset_id}
    editable(db,user,upload.version_id)
    require(upload.expires_at>now(),'上传会话已过期',409)
    require(upload.status=='uploading' or upload.status=='finalizing' and
        (upload.finalize_until is None or upload.finalize_until<now()),'文件正在完成或已关闭，请稍后重试',409)
    total=math.ceil(upload.size/upload.part_size)
    require(set(upload.parts)=={str(i) for i in range(1,total+1)},'仍有分片未上传',409)
    token=uid();upload.status='finalizing';upload.finalize_token=token;upload.finalize_until=now()+timedelta(minutes=5)
    key,storage_id=upload.object_key,upload.storage_upload_id
    parts=[{'PartNumber':i,'ETag':upload.parts[str(i)]['etag']} for i in range(1,total+1)]
    db.commit();db.expire_all()
    try:
        request.app.state.storage.complete(key,storage_id,parts)
    except Exception:
        db.execute(update(Upload).where(Upload.id==upload_id,Upload.finalize_token==token,Upload.status=='finalizing')
            .values(status='uploading',finalize_token=None,finalize_until=None))
        db.commit();raise
    # Fresh authorization after remote I/O; an expired producer cannot publish bytes.
    user=resolve_upload_user(request,db,upload_id)
    upload=own_upload(db,user,upload_id)
    if upload.status=='completed':return {'asset_id':upload.asset_id}
    version,ds=editable(db,user,upload.version_id)
    require(upload.status=='finalizing' and upload.finalize_token==token and upload.expires_at>now(),
            '上传完成领取已变化或授权已撤销',409)
    asset=Asset(version_id=version.id,filename=upload.filename,object_key=upload.object_key,
        size=upload.size,sha256=upload.sha256,preview_allowed=upload.preview_allowed)
    db.add(asset);db.flush()
    upload.status,upload.asset_id='completed',asset.id
    upload.finalize_token,upload.finalize_until=None,None
    db.execute(update(Space).where(Space.id==ds.space_id).values(
        reserved_bytes=Space.reserved_bytes-upload.size,used_bytes=Space.used_bytes+upload.size))
    job=Job(space_id=ds.space_id,user_id=user.id,version_id=version.id,kind='scan',
        parameters={'asset_id':asset.id},idempotency_key=f'scan:{asset.id}')
    db.add(job);db.flush()
    from .models import ImportItem
    item=db.scalar(select(ImportItem).where(ImportItem.upload_id==upload.id))
    if item:item.asset_id,item.scan_job_id=asset.id,job.id
    audit(db,user,'upload.complete',asset.id,ds.space_id);db.commit()
    from .tasks import dispatch
    dispatch(request.app,job.id)
    return {'asset_id':asset.id,'job_id':job.id}


def finish_abort(db,storage,upload_id):
    upload=fetch(db,Upload,upload_id)
    if upload.status!='aborting':return
    key,storage_id=upload.object_key,upload.storage_upload_id
    db.commit()
    storage.abort(key,storage_id);storage.delete(key)
    upload=fetch(db,Upload,upload_id,lock=True)
    if upload.status=='aborting':
        ds=db.get(Dataset,db.get(Version,upload.version_id).dataset_id)
        db.execute(update(Space).where(Space.id==ds.space_id).values(reserved_bytes=Space.reserved_bytes-upload.size))
        upload.status='expired' if upload.expires_at<now() else 'aborted'
        upload.finalize_token,upload.finalize_until=None,None
        db.commit()


@router.delete("/uploads/{upload_id}")
def abort_upload(upload_id: str, request: Request, db=Depends(get_db), user=Depends(upload_identity)):
    upload=own_upload(db,user,upload_id)
    require(upload.status in {'uploading','finalizing','aborting','aborted','expired'},'该上传已完成',409)
    require(upload.status!='finalizing' or upload.expires_at<now() or not upload.finalize_until or upload.finalize_until<now(),
            '文件正在完成，暂不能取消，请稍后重试',409)
    if upload.status in {'aborted','expired'}:return {'message':'上传已终止'}
    if upload.status!='aborting':
        upload.status='aborting'
        ds=db.get(Dataset,db.get(Version,upload.version_id).dataset_id)
        audit(db,user,'upload.abort',upload.id,ds.space_id)
    db.commit()
    finish_abort(db,request.app.state.storage,upload_id)
    return {'message':'上传已终止'}


@router.delete("/assets/{asset_id}")
def remove_draft_asset(asset_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    asset = fetch(db, Asset, asset_id)
    version, ds = editable(db, user, asset.version_id)
    require(not version.card.get('_review_pipeline') or Path(asset.filename).suffix.lower() not in {'.jpg','.jpeg','.png','.webp'},'联合标注已固定图片范围，请新建版本修改图片',409)
    from .tasks import release_reservation
    for job in db.scalars(select(Job).where(Job.version_id == version.id,
                          Job.status.in_(["queued", "running"])).with_for_update()):
        release_reservation(db, job)
        job.status, job.finished_at = "cancelled", now()
    # Acquire running-job locks before reading references: a vision worker may have
    # committed a checkpoint while deletion waited for its job lock. On conflict,
    # the whole transaction rolls back, including cancellation and quota release.
    from .models import ImportItem
    references = ((ImportItem, ImportItem.asset_id), (ImageReviewItem, ImageReviewItem.asset_id),
                  (ImageReviewImage, ImageReviewImage.asset_id),
                  (ImageReviewBatch, ImageReviewBatch.report_asset_id),
                  (VisionCheckpoint, VisionCheckpoint.asset_id))
    from .models import EvaluationSet
    references=(*references,(EvaluationSet,EvaluationSet.truth_asset_id))
    require(not any(db.scalar(select(model.id).where(column == asset.id).limit(1))
                    for model, column in references),
            '文件已被复核记录或模型检查点引用，不能直接删除；调整数据请创建新版本', 409)
    # Persist a non-publishable state before the irreversible object delete. A failed
    # delete/commit can then be retried without exposing a clean record with no bytes.
    asset.status = "removing"
    from .thumbnails import delete_for_asset
    delete_for_asset(db,asset,ds.space_id,request.app.state.storage)
    db.commit()
    request.app.state.storage.delete(asset.object_key)
    version, ds = editable(db, user, asset.version_id)
    asset = db.scalar(select(Asset).where(Asset.id == asset_id)
                      .execution_options(populate_existing=True).with_for_update())
    if asset is None:
        return {"message": "草稿文件已移除"}
    # Retain the upload record as history, but do not leave a dangling asset reference.
    db.execute(update(Upload).where(Upload.asset_id == asset.id).values(asset_id=None, status="removed"))
    db.execute(update(Space).where(Space.id == ds.space_id).values(used_bytes=Space.used_bytes - asset.size))
    audit(db, user, "asset.remove_draft", asset.id, ds.space_id)
    db.delete(asset)
    db.commit()
    return {"message": "草稿文件已移除"}
