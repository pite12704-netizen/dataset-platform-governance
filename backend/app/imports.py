"""【学习标注：批量接入】批次范围凭据、分页清单、受控续传和发布前清点。"""
import hashlib
import hmac
import io
import json
import logging
import secrets
import zipfile
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field, model_validator
from sqlalchemy import func, select, update

from .common import audit, fetch, public_row, require
from .db import get_db, now, uid
from .models import Asset, Dataset, ImportBatch, ImportItem, Job, Membership, Space, Upload, User, Version
from .policy import manage_dataset
from .schemas import Input, UploadInput
from .security import interactive_user, resolve_interactive_user, token_hash

router = APIRouter(prefix='/imports', tags=['批量导入'])
logger = logging.getLogger(__name__)


class CreateImport(Input):
    version_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    source_id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.-]+$')
    expected_files: int = Field(ge=1, le=100000)
    expected_bytes: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ManifestItem(UploadInput):
    idempotency_key: str = 'manifest'
    external_id: str = Field(min_length=1, max_length=200)
    source_revision: str = Field(default='', max_length=200)
    source_group: str = Field(default='', max_length=200)
    source: str = Field(default='', max_length=2000)
    license: str = Field(default='', max_length=1000)
    source_annotation: dict = Field(default_factory=dict)

    @model_validator(mode='after')
    def bounded_metadata(self):
        require(not self.preview_allowed, '批量导入默认不授权公开样例', 422)
        require(len(json.dumps(self.source_annotation, ensure_ascii=False).encode()) <= 4096,
                '单条来源标注最多 4KiB', 422)
        require(not any(ord(c) < 32 for c in self.external_id), '来源主键含控制字符', 422)
        return self


class ManifestPage(Input):
    items: list[ManifestItem] = Field(min_length=1, max_length=100)


class PartialAcceptance(Input):
    confirmed: bool
    reason: str = Field(min_length=5,max_length=2000)
    expected_clean_files: int = Field(ge=1,le=100000)


def accepted_manifest(db, batch):
    rows=db.execute(select(ImportItem.external_id,Asset.id,Asset.sha256,Asset.size)
        .join(Asset,ImportItem.asset_id==Asset.id).where(ImportItem.batch_id==batch.id,Asset.status=='clean')
        .order_by(ImportItem.external_id)).all()
    from .quality_contract import digest
    return digest([tuple(r) for r in rows])


def access(request, db, batch_id, lock=False):
    batch = fetch(db, ImportBatch, batch_id)
    token = request.headers.get('authorization', '').removeprefix('Bearer ')
    scoped = token.startswith('dp_ing_')
    if scoped:
        require(not batch.revoked and batch.expires_at > now()
                and hmac.compare_digest(token_hash(token), batch.token_hash), '导入凭据无效或已到期', 401)
        user = fetch(db, User, batch.user_id)
        require(user.active and user.verified and not user.must_change_password, '导入账号不可用', 401)
    else:
        user = resolve_interactive_user(request, db)
    version = fetch(db, Version, batch.version_id)
    dataset = fetch(db, Dataset, version.dataset_id)
    require(user.id == batch.user_id and manage_dataset(db, user, dataset), '导入批次不存在', 404)
    require(dataset.status == 'active', '数据集不可操作', 409)
    if lock:
        from .uploads import editable
        version, dataset = editable(db, user, version.id)
        batch = fetch(db, ImportBatch, batch.id, lock=True)
        # 重新读取，阻断凭据轮换或取消与接收分片之间的竞争。
        if scoped:
            require(not batch.revoked and batch.expires_at > now()
                    and hmac.compare_digest(token_hash(token), batch.token_hash), '导入凭据已撤销', 401)
    elif scoped:
        require(version.status in {'draft', 'rejected'}, '版本已冻结', 409)
    return batch, user, dataset


def upload_actor(request, db, upload_id, lock=True):
    """导入凭据只能访问在本批次清单中登记的 Upload，普通接口不接受该凭据。"""
    item = db.scalar(select(ImportItem).where(ImportItem.upload_id == upload_id))
    require(item is not None, '上传会话不属于此导入', 404)
    batch, user, _ = access(request, db, item.batch_id, lock and request.method != 'GET')
    require(batch.status != 'cancelled', '导入已取消', 409)
    if request.method in {'PUT', 'DELETE'}:
        require(not batch.sealed, '导入清单已封存', 409)
    return user


def counts(db, batch):
    rows = db.execute(select(Asset.status, Job.status, func.count(ImportItem.id), func.coalesce(func.sum(ImportItem.size), 0))
        .select_from(ImportItem).outerjoin(Asset, ImportItem.asset_id == Asset.id)
        .outerjoin(Job, ImportItem.scan_job_id == Job.id)
        .where(ImportItem.batch_id == batch.id).group_by(Asset.status, Job.status)).all()
    result = {'registered': 0, 'registered_bytes': 0, 'uploaded': 0, 'clean': 0, 'failed': 0, 'checking': 0}
    for status, job_status, number, size in rows:
        result['registered'] += number
        result['registered_bytes'] += int(size)
        if status is not None:
            result['uploaded'] += number
            result['clean' if status == 'clean' else 'failed' if status == 'rejected' or job_status in {'failed', 'cancelled'} else 'checking'] += number
    return result


def refresh(db, batch):
    summary = counts(db, batch)
    status = batch.status
    if batch.sealed and batch.status != 'cancelled':
        status = ('completed' if summary['clean'] == batch.expected_files else
                  'partial_failed' if summary['failed'] else 'checking')
    return {**public_row(batch), 'status': status, 'counts': summary}


@router.post('', status_code=201)
def create(data: CreateImport, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    from .uploads import editable
    version, dataset = editable(db, user, data.version_id)
    require(dataset.modality in {'image','multimodal'},'图片批量导入仅支持图像或多模态数据集，请先选择正确类型',422)
    old = db.scalar(select(ImportBatch).where(ImportBatch.user_id == user.id,
                                             ImportBatch.idempotency_key == data.idempotency_key))
    if old:
        require(all(getattr(old, k) == getattr(data, k) for k in ('version_id', 'source_id', 'expected_files', 'expected_bytes')),
                '幂等键已用于其他导入清单', 409)
        return refresh(db, old)
    require(data.expected_bytes <= fetch(db, Space, dataset.space_id).quota_bytes, '清单大小超过空间总配额', 409)
    token = 'dp_ing_' + secrets.token_urlsafe(32)
    batch = ImportBatch(**data.model_dump(), user_id=user.id, token_hash=token_hash(token),
                        expires_at=now() + timedelta(hours=24))
    db.add(batch); db.flush()
    audit(db, user, 'import.create', batch.id, dataset.space_id, files=data.expected_files, bytes=data.expected_bytes)
    db.commit()
    return {**refresh(db, batch), 'token': token}


@router.get('')
def listing(version_id: str | None = None, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
            db=Depends(get_db), user=Depends(interactive_user)):
    query = (select(ImportBatch).join(Version, ImportBatch.version_id == Version.id)
             .join(Dataset, Version.dataset_id == Dataset.id)
             .join(Membership, Membership.space_id == Dataset.space_id)
             .where(ImportBatch.user_id == user.id, Dataset.owner_id == user.id, Dataset.status == 'active',
                    Membership.user_id == user.id, Membership.active.is_(True)))
    if version_id:
        query = query.where(ImportBatch.version_id == version_id)
    rows = db.scalars(query.order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc()).offset(offset).limit(limit + 1)).all()
    result = {'items': [refresh(db, row) for row in rows[:limit]], 'has_more': len(rows) > limit}
    db.commit()
    return result


@router.get('/client-kit')
def client_kit(db=Depends(get_db), user=Depends(interactive_user)):
    """仅打包固定白名单源码和无凭据示例，客户无需获得整个平台仓库。"""
    root = Path(__file__).resolve().parents[2]
    names = ['clients/__init__.py'] + ['clients/ingest/' + name for name in (
        '__init__.py', '__main__.py', 'exporter.py', 'local_source.py', 'uploader.py', 'requirements.txt', 'requirements.lock', 'source.example.json', 'README.md')]
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, (root / name).read_bytes())
    audit(db, user, 'import.client_kit'); db.commit()
    return Response(output.getvalue(), media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="dataset-ingest-client.zip"', 'Cache-Control': 'no-store'})


@router.get('/{batch_id}')
def detail(batch_id: str, request: Request, db=Depends(get_db)):
    batch, _, _ = access(request, db, batch_id)
    result = refresh(db, batch); db.commit()
    return result


@router.post('/{batch_id}/credential')
def rotate(batch_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    batch, user, dataset = access(request, db, batch_id, True)
    require(refresh(db, batch)['status'] not in {'completed', 'cancelled'}, '导入已结束', 409)
    token = 'dp_ing_' + secrets.token_urlsafe(32)
    batch.token_hash, batch.expires_at, batch.revoked = token_hash(token), now() + timedelta(hours=24), False
    audit(db, user, 'import.credential.rotate', batch.id, dataset.space_id); db.commit()
    return {'token': token, 'expires_at': batch.expires_at}


@router.post('/{batch_id}/credential/revoke')
def revoke(batch_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    batch, user, dataset = access(request, db, batch_id, True)
    batch.revoked = True
    audit(db, user, 'import.credential.revoke', batch.id, dataset.space_id); db.commit()
    return {'revoked': True}


@router.get('/{batch_id}/items')
def items(batch_id: str, request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
          db=Depends(get_db)):
    access(request, db, batch_id)
    rows = db.execute(select(ImportItem, Upload, Asset, Job).outerjoin(Upload, ImportItem.upload_id == Upload.id)
        .outerjoin(Asset, ImportItem.asset_id == Asset.id).where(ImportItem.batch_id == batch_id)
        .outerjoin(Job, ImportItem.scan_job_id == Job.id)
        .order_by(ImportItem.created_at, ImportItem.id).offset(offset).limit(limit + 1)).all()
    return {'items': [{**public_row(i), 'status': 'failed' if j and j.status in {'failed', 'cancelled'} else a.status if a else u.status if u else 'pending',
                       'error': j.error if j and j.status == 'failed' else '文件未通过检查' if a and a.status == 'rejected' else None}
                      for i, u, a, j in rows[:limit]], 'has_more': len(rows) > limit}


@router.post('/{batch_id}/items')
def register(batch_id: str, data: ManifestPage, request: Request, db=Depends(get_db)):
    batch, user, dataset = access(request, db, batch_id, True)
    require(dataset.modality in {'image','multimodal'},'图片批量导入仅支持图像或多模态数据集',422)
    require(not batch.sealed and batch.status != 'cancelled', '清单已封存或取消', 409)
    require(len({i.external_id for i in data.items}) == len(data.items), '本页存在重复来源主键', 422)
    summary = counts(db, batch)
    result = []
    for entry in data.items:
        values = entry.model_dump(exclude={'preview_allowed', 'idempotency_key'})
        require(Path(entry.filename).suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'}, '批量导入仅接收已支持的图片', 422)
        require(entry.size <= min(request.app.state.settings.max_upload_bytes, 20 * 1024**2), '图片超过接收上限', 413)
        old = db.scalar(select(ImportItem).where(ImportItem.batch_id == batch.id, ImportItem.external_id == entry.external_id))
        if old:
            require(all(getattr(old, k) == v for k, v in values.items()), '同一来源主键的清单内容已变化', 409)
        else:
            summary['registered'] += 1; summary['registered_bytes'] += entry.size
            require(summary['registered'] <= batch.expected_files and summary['registered_bytes'] <= batch.expected_bytes,
                    '登记内容超过声明的文件数或字节数', 409)
            old = ImportItem(batch_id=batch.id, **values); db.add(old); db.flush()
        result.append(public_row(old))
    audit(db, user, 'import.register', batch.id, dataset.space_id, count=len(result)); db.commit()
    return {'items': result}


@router.post('/{batch_id}/items/{item_id}/upload')
def begin_upload(batch_id: str, item_id: str, request: Request, db=Depends(get_db)):
    from .uploads import create_upload,prepare_storage,cleanup_prepared
    batch,user,_=access(request,db,batch_id)
    require(not batch.sealed and batch.status!='cancelled','导入已封存或取消',409)
    item=fetch(db,ImportItem,item_id)
    require(item.batch_id==batch.id,'文件不属于本批次',404)
    if item.upload_id:
        old=fetch(db,Upload,item.upload_id)
        if old.status=='completed' or old.status in {'uploading','finalizing'} and old.expires_at>now():return public_row(old)
        if old.status in {'uploading','finalizing','aborting'}:
            from .uploads import abort_upload
            abort_upload(old.id,request,db,user)
    data=UploadInput(filename=item.filename,size=item.size,sha256=item.sha256,
        idempotency_key='import:'+item.id+':'+uid())
    prepared=prepare_storage(batch.version_id,data,request,db,user)
    try:
        batch,user,_=access(request,db,batch_id,True)
        require(not batch.sealed and batch.status!='cancelled','导入已封存或取消',409)
        item=fetch(db,ImportItem,item_id,lock=True)
        if item.upload_id:
            old=fetch(db,Upload,item.upload_id,lock=True)
            if old.status=='completed' or old.status in {'uploading','finalizing'} and old.expires_at>now():return public_row(old)
        row=create_upload(batch.version_id,data,request,db,user,commit=False,storage_session=prepared)
        item.upload_id=row['id'];db.commit()
        return row
    except Exception:
        db.rollback();raise
    finally:cleanup_prepared(db,request,prepared)


@router.post('/{batch_id}/finalize')
def finalize(batch_id: str, request: Request, db=Depends(get_db)):
    batch, user, dataset = access(request, db, batch_id, True)
    require(batch.status != 'cancelled', '导入已取消', 409)
    summary = counts(db, batch)
    require(summary['registered'] == batch.expected_files and summary['registered_bytes'] == batch.expected_bytes
            and summary['uploaded'] == batch.expected_files, '清单或文件未完整到达，不能完成导入', 409)
    batch.sealed = True
    digest = hashlib.sha256()
    fields = ('external_id', 'source_revision', 'filename', 'size', 'sha256', 'source_group', 'source', 'license', 'source_annotation')
    for item in db.scalars(select(ImportItem).where(ImportItem.batch_id == batch.id)
                           .order_by(ImportItem.external_id, ImportItem.id).execution_options(yield_per=200)):
        digest.update(json.dumps({k: getattr(item, k) for k in fields}, sort_keys=True,
                                 ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
    batch.manifest_hash = digest.hexdigest()
    result = refresh(db, batch)
    batch.status = result['status']
    audit(db, user, 'import.finalize', batch.id, dataset.space_id, status=batch.status); db.commit()
    return result


@router.post('/{batch_id}/items/{item_id}/retry-scan')
def retry_scan(batch_id: str, item_id: str, request: Request, db=Depends(get_db)):
    batch, user, _ = access(request, db, batch_id, True)
    require(batch.status not in {'cancelled', 'completed'}, '导入已结束', 409)
    item = fetch(db, ImportItem, item_id)
    require(item.batch_id == batch.id and item.scan_job_id, '不存在可重试的检查任务', 409)
    from .tasks import retry_job
    result = retry_job(item.scan_job_id, request, db, user)
    return result


@router.post('/{batch_id}/cancel')
def cancel(batch_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    batch, user, dataset = access(request, db, batch_id, True)
    require(refresh(db, batch)['status'] != 'completed', '完整导入已完成', 409)
    summary = counts(db, batch)
    batch.status, batch.revoked, batch.sealed = 'cancelled', True, True
    batch.resolution={}
    db.execute(update(Upload).where(Upload.status.in_({'uploading','finalizing','aborting'}), Upload.id.in_(
        select(ImportItem.upload_id).where(ImportItem.batch_id == batch.id))).values(expires_at=now()))
    audit(db, user, 'import.cancel', batch.id, dataset.space_id, retained_files=summary['uploaded'],
          excluded_files=batch.expected_files - summary['uploaded']); db.commit()
    return {**public_row(batch), 'counts': summary, 'cleanup': '未完成分片由后台到期回收；已接受资产保留并继续检查'}


@router.post('/{batch_id}/accept-partial')
def accept_partial(batch_id:str,data:PartialAcceptance,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    batch,user,dataset=access(request,db,batch_id,True)
    require(batch.status=='cancelled' and data.confirmed,'请先停止导入并明确确认接收子集',409)
    summary=counts(db,batch)
    require(not summary['checking'] and summary['clean']==data.expected_clean_files and
            summary['clean']==summary['uploaded'],'已接受文件仍在检查或存在失败，请先处理并刷新数量',409)
    require(not db.scalar(select(Upload.id).where(Upload.status.in_({'uploading','finalizing','aborting'}),Upload.id.in_(
        select(ImportItem.upload_id).where(ImportItem.batch_id==batch.id))).limit(1)),
        '未完成分片尚未回收，请稍后重试',409)
    batch.resolution={'kind':'accepted_partial','actor_id':user.id,'reason':data.reason,
        'accepted_files':summary['clean'],'original_expected_files':batch.expected_files,
        'omitted_files':batch.expected_files-summary['clean'],'accepted_manifest':accepted_manifest(db,batch),
        'accepted_at':now().isoformat()+'Z'}
    audit(db,user,'import.partial.accept',batch.id,dataset.space_id,resolution=batch.resolution)
    db.commit()
    return refresh(db,batch)


def require_complete(db, version_id):
    for batch in db.scalars(select(ImportBatch).where(ImportBatch.version_id == version_id)):
        state=refresh(db,batch)
        if state['status']=='completed':continue
        if state['status']=='cancelled':
            if state['counts']['uploaded']==0:continue
            resolution=batch.resolution or {}
            require(resolution.get('kind')=='accepted_partial' and state['counts']['uploaded']==state['counts']['clean']
                    and resolution.get('accepted_files')==state['counts']['clean']
                    and resolution.get('accepted_manifest')==accepted_manifest(db,batch),
                    '导入只完成部分；请确认最终接收清单后再发布',409)
            continue
        require(False,'批量导入尚未完成，请检查缺失或失败文件',409)
