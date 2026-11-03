"""【学习标注：图像复核】原预测 → 修订并自动送审 → 另一人复核 → 审核标签导出。

标注附属于固定版本，predicted 永不更新；每次变更使用 revision 条件更新并追加事件。
角色只允许执行操作，读取图像仍必须通过版本内容授权。此模块不接受文件路径。
"""
import base64
import io
import json
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from .common import audit, fetch, public_row, require
from .db import get_db, now, uid
from .models import Asset, Dataset, ImageReviewBatch as Batch, ImageReviewItem as Item, ImageReviewEvent as Event, Membership, Version
from .models import ImageReviewImage as ReviewImage, ImageReviewImageEvent as ImageEvent
from .policy import can_read_content, manage_dataset, member
from .schemas import Input
from .security import interactive_user
from .concurrency import media_slot

router = APIRouter(prefix="/image-reviews", tags=["图像标注与复核"])


class Labels(Input):
    hat: Literal["hat", "no_hat", "unclear"]
    sleeve: Literal["short_sleeves", "long_sleeves", "sleeveless", "unclear"]
    upper_color: Literal["white", "black", "red", "blue", "green", "yellow", "other_or_unclear"]


class Annotation(Input):
    # bbox 永远是 EXIF 方向校正后的原图像素坐标 [x, y, width, height]。
    bbox: list[float] = Field(min_length=4, max_length=4)
    labels: dict
    category: str | None = Field(default=None,max_length=40)

    def model_dump(self,*args,**kwargs):
        kwargs.setdefault('exclude_none',True)
        return super().model_dump(*args,**kwargs)

    @model_validator(mode="after")
    def valid_box(self):
        import math
        x, y, w, h = self.bbox
        if not all(math.isfinite(n) for n in self.bbox) or min(x, y) < 0 or min(w, h) <= 0:
            raise ValueError("人物框必须是有限正面积坐标")
        if self.category is None:Labels.model_validate(self.labels)
        return self


class ImportInput(Input):
    report_asset_id: str


class EditInput(Input):
    revision: int = Field(ge=0)
    claim_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    annotation: Annotation
    # 常规属性纠正由结构化的 before/after 记录说明，不强迫审核人重复填写文字原因。
    note: str = Field(default="", max_length=1000)


class Transition(Input):
    revision: int = Field(ge=0)
    claim_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    action: Literal["submit", "approve", "reject"]
    note: str = Field(default="", max_length=1000)
    human_confirmed: bool = False


class ClaimNext(Input):
    after_item_id: str | None = Field(default=None, max_length=32)


class LeaseInput(Input):
    claim_token: str = Field(pattern=r"^[a-f0-9]{32}$")


def public_item(item):
    # 领取凭证仅发给领取者，目录、历史和导出都不能泄漏它。
    return public_row(item, exclude=("claim_token",))


def eligible(item, user):
    return item.status in {"pending", "rejected"} or item.status == "submitted" and item.editor_id != user.id


def available_claim():
    return or_(Item.claimed_by_id.is_(None), Item.claim_expires_at.is_(None), Item.claim_expires_at <= now())


def assert_lease(item, user, token):
    require(item.claimed_by_id == user.id and item.claim_token == token
            and item.claim_expires_at and item.claim_expires_at > now(),
            "领取已到期或已由其他审核人接手，请重新领取", 409)


def checked_annotation(data, width, height, template=None):
    annotation = Annotation.model_validate(data)
    x, y, w, h = annotation.bbox
    require(x + w <= width + 0.01 and y + h <= height + 0.01, "人物框超出图像边界", 422)
    value=annotation.model_dump()
    if template:
        from .review_templates import check_annotation
        return check_annotation(value,template)
    require(annotation.category is None,'此批次未绑定通用目标模板',422)
    return value


def read_image(asset, storage):
    require(asset.status == "clean", "图片检查尚未通过", 409)
    require(0 < asset.size <= 20 * 1024**2, "图片大小必须在 20MB 以内", 413)
    raw = b"".join(storage.chunks(asset.object_key, 0, asset.size - 1))
    import hashlib
    require(hashlib.sha256(raw).hexdigest() == asset.sha256, "图片完整性校验失败", 409)
    with Image.open(io.BytesIO(raw)) as image:
        require(image.width * image.height <= 40_000_000, "图片超过 4000 万像素", 413)
        return ImageOps.exif_transpose(image).convert("RGB")


def access(db, user, batch, settings):
    version = fetch(db, Version, batch.version_id)
    ds = fetch(db, Dataset, version.dataset_id)
    membership = member(db, user, ds.space_id)
    owner = manage_dataset(db, user, ds)
    from .joint_review import flags
    annotate_staff,review_staff=flags(db,user,version)
    active_batch = batch.status not in {'draft', 'discarded'}
    require(ds.status == "active" and (owner or active_batch and (annotate_staff or review_staff)),
            "复核批次不存在或尚未提交审核", 403)
    is_creator = batch.creator_id == user.id
    return version, ds, {
        "can_submit_batch": bool(owner and batch.status == "draft" and version.status in {'draft', 'rejected'}),
        "can_discard_batch": bool(owner and batch.status == "draft" and version.status in {'draft', 'rejected'}),
        "can_edit": bool(active_batch and annotate_staff and not is_creator and version.status in {'draft', 'rejected'}),
        "can_review": bool(active_batch and review_staff and not is_creator and version.status in {'draft', 'rejected'}),
    }


def create_batch(db, version_id, creator_id, rows, provenance, **references):
    """Worker 与报告导入共用的事务入口；调用方提交后才可见。"""
    require(len(rows) <= 2400, "复核批次最多 2400 个人物", 413)
    batch = Batch(version_id=version_id, creator_id=creator_id, provenance=provenance, **references)
    from .review_templates import for_version,digest
    template=for_version(fetch(db,Version,version_id))
    require((provenance.get('review_template') is None and template is None) or
        template is not None and provenance.get('review_template')==template and provenance.get('template_digest')==digest(template),
        '识别模板与版本模板不一致',409)
    db.add(batch)
    db.flush()
    for row in rows:
        annotation = checked_annotation(row["annotation"], row["width"], row["height"],template)
        db.add(Item(batch_id=batch.id, asset_id=row["asset_id"], person_key=row["person_key"],
                    width=row["width"], height=row["height"], predicted=annotation, annotation=annotation))
    from .review_coverage import coverage
    for asset_id, dimensions in coverage(rows,provenance).items():
        db.add(ReviewImage(batch_id=batch.id, asset_id=asset_id,
                           width=dimensions['width'], height=dimensions['height']))
    db.flush()
    return batch


def item_rows(db, batch):
    return db.scalars(select(Item).where(Item.batch_id == batch.id).order_by(Item.asset_id, Item.person_key)).all()


def summary(db, batch):
    rows = item_rows(db, batch)
    images = db.scalars(select(ReviewImage).where(ReviewImage.batch_id == batch.id)).all()
    return {**public_row(batch), "counts": {status: sum(i.status == status for i in rows)
            for status in ("pending", "submitted", "approved", "rejected", "excluded")}, "total": len(rows),
            'image_counts': {status: sum(i.status == status for i in images)
                             for status in ('pending', 'submitted', 'approved')}, 'image_total': len(images)}


def mark_submitted(db, user, batch):
    rows = item_rows(db, batch)
    require(rows or db.scalar(select(ReviewImage.id).where(ReviewImage.batch_id == batch.id)),
            "空批次不能提交", 409)
    for item in rows:
        require(item.status == "pending" and item.editor_id is None, "批次已开始处理，不能重复整批提交", 409)
        before = {"annotation": item.annotation, "status": item.status, "editor_id": None, "reviewer_id": None}
        item.status, item.revision = "submitted", item.revision + 1
        after = {"annotation": item.annotation, "status": item.status, "editor_id": None, "reviewer_id": None}
        db.add(Event(item_id=item.id, actor_id=user.id, revision=item.revision, action="batch_submit",
                     before=before, after=after, note="数据负责人整批提交人工审核"))
    batch.status, batch.submitted_by_id, batch.submitted_at = "submitted", user.id, now()
    return len(rows)


def ensure_unique_review_assets(db, version_id, drafts):
    """调用方先锁版本，序列化同版本送审，避免不同模型批次重复审核原图。"""
    submitted_assets = set(db.scalars(select(ReviewImage.asset_id).join(Batch, ReviewImage.batch_id == Batch.id)
                                    .where(Batch.version_id == version_id, Batch.status.not_in(['draft', 'discarded']))))
    for batch in drafts:
        assets = set(db.scalars(select(ReviewImage.asset_id).where(ReviewImage.batch_id == batch.id)))
        require(not submitted_assets.intersection(assets),
                "同一版本的图片已在其他送审批次中，请选择一个识别结果送审；重新评测请创建新版本", 409)
        submitted_assets.update(assets)


@router.get("")
def batches(request: Request, version_id: str | None = None, db=Depends(get_db), user=Depends(interactive_user)):
    # 先按当前组织成员身份过滤，再截取队列，其他组织不能挤掉本人的待办。
    membership = select(Membership.id).where(Membership.space_id == Dataset.space_id,
        Membership.user_id == user.id, Membership.active.is_(True)).exists()
    reviewer = select(Membership.id).where(Membership.space_id == Dataset.space_id,
        Membership.user_id == user.id, Membership.active.is_(True), Membership.role.in_(['admin', 'reviewer'])).exists()
    query = select(Batch).join(Version, Batch.version_id == Version.id).join(Dataset, Version.dataset_id == Dataset.id).where(
        Dataset.status == 'active', or_(and_(Dataset.owner_id == user.id, membership),
        and_(Batch.status.not_in(['draft', 'discarded']), reviewer))).order_by(Batch.created_at.desc(), Batch.id.desc())
    if version_id:
        query = query.where(Batch.version_id == version_id)
    result, asset_sets = [], []
    for batch in db.scalars(query.limit(200)):
        try:
            _, _, permissions = access(db, user, batch, request.app.state.settings)
            result.append({**summary(db, batch), **permissions})
            asset_sets.append(set(db.scalars(select(Item.asset_id).where(Item.batch_id == batch.id)))
                              if batch.status != 'discarded' else set())
        except Exception as exc:
            from fastapi import HTTPException
            if not isinstance(exc, HTTPException) or exc.status_code != 403:
                raise
    for index, row in enumerate(result):
        row["overlaps_other_batch"] = any(index != other and bool(asset_sets[index] & candidate)
                                           for other, candidate in enumerate(asset_sets))
    return result


@router.post("/import", status_code=201)
def import_report(data: ImportInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    asset = fetch(db, Asset, data.report_asset_id)
    version = fetch(db, Version, asset.version_id)
    ds = fetch(db, Dataset, version.dataset_id)
    require(manage_dataset(db, user, ds) and can_read_content(db, user, version, request.app.state.settings))
    old = db.scalar(select(Batch).where(Batch.report_asset_id == asset.id))
    if old:
        return summary(db, old)
    require(asset.status == "clean" and 0 < asset.size <= 2 * 1024**2, "报告未通过检查或超出 2MB", 409)
    try:
        raw = b"".join(request.app.state.storage.chunks(asset.object_key, 0, asset.size - 1))
        import hashlib
        require(hashlib.sha256(raw).hexdigest() == asset.sha256, "报告完整性校验失败", 409)
        report = json.loads(raw)
        from .review_templates import for_version,digest
        template=for_version(version)
        require(report.get('schema_version')==(2 if template else 1) and isinstance(report.get('images'),list),'报告格式或模板版本不匹配',422)
        if template:require(report.get('template_digest')==digest(template),'导入报告的模板摘要不匹配',409)
        require(0 < len(report["images"]) <= 100, "每批次支持 1–100 张图像", 413)
        rows, seen, scans = [], set(), []
        for image in report["images"]:
            source = fetch(db, Asset, image["asset_id"])
            require(source.version_id == version.id, "报告不能引用其他版本的图像", 422)
            require(source.id not in seen, "报告重复引用图像", 422)
            seen.add(source.id)
            actual = read_image(source, request.app.state.storage)
            width, height = actual.size
            require((width, height) == (image["width"], image["height"]), "报告尺寸与图片不一致", 422)
            objects=image['objects'] if template else image['persons']
            scans.append({'asset_id': source.id, 'width': width, 'height': height,
                          'persons': len(objects), 'truncated': False})
            require(len(objects) <= 24, "单张图片最多 24 个目标", 413)
            people = set()
            for person in objects:
                key = str(person["id"])
                require(key and len(key) <= 64 and key not in people, "人物标识无效或重复", 422)
                people.add(key)
                value={'bbox':person['bbox'],'category':person['category'],'labels':person['labels']} if template else {
                    'bbox':person['bbox'],'labels':{name:person['attributes'][name]['label'] for name in Labels.model_fields}}
                annotation = checked_annotation(value,width,height,template)
                rows.append(dict(asset_id=source.id, person_key=key, width=width, height=height, annotation=annotation))
        from .workflows import locked_version
        version, ds = locked_version(db, version.id)
        require(manage_dataset(db, user, ds) and ds.status == 'active', '数据集不可修改', 403)
        require(version.status in {'draft', 'rejected'}, '版本已冻结，不能新增识别批次', 409)
        # File reads happen outside the lock. Refresh them before adding references
        # so a concurrent removal cannot leave review records pointing to lost bytes.
        input_ids = seen | {asset.id}
        current_inputs = db.scalars(select(Asset).where(Asset.id.in_(input_ids))
                                   .execution_options(populate_existing=True)).all()
        require(len(current_inputs) == len(input_ids) and
                all(a.version_id == version.id and a.status == 'clean' for a in current_inputs),
                '报告或原图在导入期间已变更，请刷新后重试', 409)
        batch = create_batch(db, version.id, user.id, rows,
            {"origin": "imported_report", "model": str(report.get("model_name", "unknown"))[:200],
             **({'review_template':template,'template_digest':digest(template)} if template else {}),
             "box_source": "imported_unverified", "images": len(seen), "scans": scans,
             "report_sha256": asset.sha256}, report_asset_id=asset.id)
        audit(db, user, "image_review.import", batch.id, ds.space_id, persons=len(rows))
        db.commit()
    except (KeyError, TypeError, ValueError, ValidationError):
        db.rollback()
        require(False, "报告结构或标注无效", 422)
    except IntegrityError:
        db.rollback()
        batch = db.scalar(select(Batch).where(Batch.report_asset_id == asset.id))
        require(batch is not None, "导入冲突，请刷新重试", 409)
    return summary(db, batch)


@router.post("/versions/{version_id}/submit")
def submit_version_batches(version_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    """一次提交同一版本的全部草稿批次，支持大数据集拆成多个模型批次。"""
    from .workflows import locked_version
    version, ds = locked_version(db, version_id)
    require(manage_dataset(db, user, ds) and ds.status == "active", "只有数据负责人可以整套送审")
    require(version.status in {'draft', 'rejected'}, '版本已冻结', 409)
    batches = db.scalars(select(Batch).where(Batch.version_id == version.id).order_by(Batch.id)
                         .with_for_update()).all()
    drafts = [batch for batch in batches if batch.status == "draft"]
    require(drafts, "没有待提交的识别批次", 409)
    ensure_unique_review_assets(db, version.id, drafts)
    persons = sum(mark_submitted(db, user, batch) for batch in drafts)
    audit(db, user, "image_review.version_submit", version.id, ds.space_id,
          batches=len(drafts), persons=persons)
    db.commit()
    return {"version_id": version.id, "submitted_batches": len(drafts), "persons": persons,
            "batches": [summary(db, batch) for batch in drafts]}


@router.get("/{batch_id}")
def detail(batch_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    batch = fetch(db, Batch, batch_id)
    version, ds, permissions = access(db, user, batch, request.app.state.settings)
    rows = item_rows(db, batch)
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.version_id == version.id))}
    return {**summary(db, batch), "dataset_name": ds.name, "version_number": version.number, **permissions,
            "lease_seconds": request.app.state.settings.review_lease_seconds,
            "items": [{**public_item(row), "filename": assets[row.asset_id].filename} for row in rows],
            'images': [{**public_row(row, exclude=('claim_token',)), 'filename': assets[row.asset_id].filename}
                       for row in db.scalars(select(ReviewImage).where(ReviewImage.batch_id == batch.id).order_by(ReviewImage.id))]}


@router.post("/{batch_id}/submit")
def submit_batch(batch_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    """数据负责人把整批模型候选送入组织复核队列，不逐条冒充人工标注。"""
    version_id = db.scalar(select(Batch.version_id).where(Batch.id == batch_id))
    require(version_id, "批次不存在", 404)
    from .workflows import locked_version
    locked_version(db, version_id)
    batch = fetch(db, Batch, batch_id, lock=True)
    _, ds, permissions = access(db, user, batch, request.app.state.settings)
    require(permissions["can_submit_batch"], "只有数据负责人可以提交草稿批次")
    ensure_unique_review_assets(db, version_id, [batch])
    persons = mark_submitted(db, user, batch)
    audit(db, user, "image_review.batch_submit", batch.id, ds.space_id, persons=persons)
    db.commit()
    return summary(db, batch)


@router.post('/{batch_id}/discard')
def discard_batch(batch_id: str, db=Depends(get_db), user=Depends(interactive_user)):
    """保留未采用的模型实验记录；明确弃用草稿才能从发布门槛中排除。"""
    version_id = db.scalar(select(Batch.version_id).where(Batch.id == batch_id))
    require(version_id, '批次不存在', 404)
    from .workflows import locked_version
    version, ds = locked_version(db, version_id)
    batch = fetch(db, Batch, batch_id, lock=True)
    require(manage_dataset(db, user, ds) and ds.status == 'active', '只有数据负责人可以弃用草稿')
    if batch.status == 'discarded':
        return summary(db, batch)
    require(version.status in {'draft', 'rejected'} and batch.status == 'draft',
            '只能弃用可编辑版本中尚未送审的草稿；已开始的审核必须完成', 409)
    assets = set(db.scalars(select(ReviewImage.asset_id).where(ReviewImage.batch_id == batch.id)))
    covered = set(db.scalars(select(ReviewImage.asset_id).join(Batch, ReviewImage.batch_id == Batch.id).where(
        Batch.version_id == version.id, Batch.id != batch.id, Batch.status != 'discarded')))
    require(assets and assets <= covered, '只能弃用重复草稿，必须保留覆盖这些图片的另一套识别候选', 409)
    batch.status = 'discarded'
    audit(db, user, 'image_review.discard', batch.id, ds.space_id, version_id=version.id)
    db.commit()
    return summary(db, batch)


@router.get("/{batch_id}/assets/{asset_id}/preview", dependencies=[Depends(media_slot)])
def review_preview(batch_id: str, asset_id: str, request: Request, db=Depends(get_db),
                   user=Depends(interactive_user)):
    """复核岗位只读取当前批次图片；不会获得版本下载授权。"""
    batch = fetch(db, Batch, batch_id)
    version, ds, _ = access(db, user, batch, request.app.state.settings)
    item = db.scalar(select(ReviewImage.id).where(ReviewImage.batch_id == batch.id, ReviewImage.asset_id == asset_id))
    require(item is not None, "图片不属于该复核批次", 404)
    asset = fetch(db, Asset, asset_id)
    require(asset.version_id == version.id, "图片不属于该版本", 404)
    from .preview_cache import thumbnail
    output=thumbnail(request,asset)
    audit(db, user, "image_review.preview", asset.id, ds.space_id, batch_id=batch.id)
    db.commit()
    return {"kind": "image", "content": "data:image/jpeg;base64," + base64.b64encode(output).decode()}


def item_access(db, user, item_id, settings):
    item = fetch(db, Item, item_id)
    batch = fetch(db, Batch, item.batch_id)
    version, ds, permissions = access(db, user, batch, settings)
    asset = fetch(db, Asset, item.asset_id)
    require(asset.version_id == version.id and asset.status == "clean", "图像不可用于复核", 409)
    return item, ds, permissions


def locked_item_access(db, user, item_id, settings):
    # 所有领取、续期和保存均按 batch → item 顺序锁定，防止死锁及批次完成状态竞态。
    batch_id = db.scalar(select(Item.batch_id).where(Item.id == item_id))
    require(batch_id, "标注不存在", 404)
    from .workflows import locked_version
    locked_version(db, db.scalar(select(Batch.version_id).where(Batch.id == batch_id)))
    batch = fetch(db, Batch, batch_id, lock=True)
    item, ds, permissions = item_access(db, user, item_id, settings)
    require(not fetch(db,Version,batch.version_id).card.get('_review_pipeline'),'联合作业请使用整图标注/审核入口，不能绕过重复判定',409)
    return item, batch, ds, permissions


def claim(db, user, batch, item, settings, ds):
    require(not fetch(db,Version,batch.version_id).card.get('_review_pipeline'),'联合作业请使用整图工作台',409)
    page = db.scalar(select(ReviewImage).where(ReviewImage.batch_id == batch.id, ReviewImage.asset_id == item.asset_id))
    require(not page or not page.claim_expires_at or page.claim_expires_at <= now(),
            '此图片正在进行整图复核，请选择其他图片', 409)
    require(eligible(item, user), "该标注已完成或需要另一位审核人确认", 409)
    timestamp = now()
    own = item.claimed_by_id == user.id and item.claim_expires_at and item.claim_expires_at > timestamp
    require(own or not item.claimed_by_id or not item.claim_expires_at or item.claim_expires_at <= timestamp,
            "该标注已被其他审核人领取，请领取下一条", 409)
    # 同一审核人在同一批次保留一个任务，切换条目时自动释放自己的旧任务。
    db.execute(update(Item).where(Item.batch_id == batch.id, Item.claimed_by_id == user.id, Item.id != item.id)
               .values(claimed_by_id=None, claim_token=None, claim_expires_at=None))
    item.claimed_by_id = user.id
    item.claim_token = item.claim_token if own else uid()
    item.claim_expires_at = timestamp + timedelta(seconds=settings.review_lease_seconds)
    audit(db, user, "image_review.claim", item.id, ds.space_id, batch_id=batch.id)
    db.commit()
    return {"item": {**public_item(item), "filename": db.get(Asset, item.asset_id).filename},
            "claim_token": item.claim_token, "lease_seconds": settings.review_lease_seconds}


@router.post("/{batch_id}/claim-next")
def claim_next(batch_id: str, data: ClaimNext, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    settings = request.app.state.settings
    from .workflows import locked_version
    version_id = db.scalar(select(Batch.version_id).where(Batch.id == batch_id))
    require(version_id, '批次不存在', 404)
    locked_version(db, version_id)
    batch = fetch(db, Batch, batch_id, lock=True)
    _, ds, permissions = access(db, user, batch, settings)
    require(permissions["can_review"], "缺少领取审核任务权限")
    candidates = select(Item).where(Item.batch_id == batch.id,
        ~select(ReviewImage.id).where(ReviewImage.batch_id == batch.id,
            ReviewImage.asset_id == Item.asset_id, ReviewImage.claim_expires_at > now()).exists(),
        or_(Item.status.in_(["pending", "rejected"]),
            (Item.status == "submitted") & or_(Item.editor_id.is_(None), Item.editor_id != user.id)))
    # 重复点击/响应丢失/同账号多标签页，都返回当前仍有效的同一份领取。
    item = db.scalar(candidates.where(Item.claimed_by_id == user.id, Item.claim_expires_at > now())
                     .order_by(Item.id).limit(1))
    if not item:
        queue = candidates.where(available_claim()).order_by(Item.asset_id, Item.person_key, Item.id)
        if data.after_item_id:
            cursor = db.scalar(select(Item).where(Item.id == data.after_item_id, Item.batch_id == batch.id))
            if cursor:
                from sqlalchemy import tuple_
                item = db.scalar(queue.where(tuple_(Item.asset_id, Item.person_key, Item.id) >
                                            (cursor.asset_id, cursor.person_key, cursor.id)).limit(1))
        item = item or db.scalar(queue.limit(1))
    if not item:
        return {"item": None, "message": "暂无可领取任务，剩余记录可能正由其他人审核或等待独立复核"}
    return claim(db, user, batch, item, settings, ds)


@router.post("/items/{item_id}/claim")
def claim_item(item_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    item, batch, ds, permissions = locked_item_access(db, user, item_id, request.app.state.settings)
    require(permissions["can_review"], "缺少领取审核任务权限")
    return claim(db, user, batch, item, request.app.state.settings, ds)


@router.post("/items/{item_id}/renew")
def renew_claim(item_id: str, data: LeaseInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    item, _, _, permissions = locked_item_access(db, user, item_id, request.app.state.settings)
    require(permissions["can_review"] and eligible(item, user), "领取资格已失效")
    assert_lease(item, user, data.claim_token)
    item.claim_expires_at = now() + timedelta(seconds=request.app.state.settings.review_lease_seconds)
    db.commit()
    return {"claim_expires_at": item.claim_expires_at}


@router.post("/items/{item_id}/release")
def release_claim(item_id: str, data: LeaseInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    item, _, ds, permissions = locked_item_access(db, user, item_id, request.app.state.settings)
    require(permissions["can_review"])
    # 旧页面重复关闭或过期释放不得清除新审核人的领取。
    if item.claimed_by_id == user.id and item.claim_token == data.claim_token:
        item.claimed_by_id, item.claim_token, item.claim_expires_at = None, None, None
        audit(db, user, "image_review.release", item.id, ds.space_id)
        db.commit()
    return {"released": True}


def change(db, user, item, batch, ds, revision, token, action, note, values):
    page = db.scalar(select(ReviewImage).where(ReviewImage.batch_id == batch.id, ReviewImage.asset_id == item.asset_id))
    require(not page or not page.claim_expires_at or page.claim_expires_at <= now(),
            '此图片正在进行整图复核，请稍后重试', 409)
    if page and action != 'approve':
        before_page={'status':page.status,'snapshot':page.snapshot}
        page.status, page.snapshot, page.reviewer_id = 'pending', {}, None
        page.revision += 1
        db.add(ImageEvent(image_id=page.id,actor_id=user.id,revision=page.revision,action='person_changed',
            before=before_page,after={'status':page.status,'snapshot':page.snapshot}))
    assert_lease(item, user, token)
    require(item.revision == revision, "内容已被其他人更新，请刷新后重试", 409)
    before = {"annotation": item.annotation, "status": item.status, "editor_id": item.editor_id,
              "reviewer_id": item.reviewer_id}
    changed = db.execute(update(Item).where(Item.id == item.id, Item.revision == revision,
                         Item.claimed_by_id == user.id, Item.claim_token == token, Item.claim_expires_at > now())
                         .values(**values, revision=revision + 1,
                                 claimed_by_id=None, claim_token=None, claim_expires_at=None))
    require(changed.rowcount == 1, "并发更新冲突，请刷新后重试", 409)
    db.refresh(item)
    after = {"annotation": item.annotation, "status": item.status, "editor_id": item.editor_id,
             "reviewer_id": item.reviewer_id}
    db.add(Event(item_id=item.id, actor_id=user.id, revision=item.revision, action=action,
                 before=before, after=after, note=note))
    remaining = db.scalar(select(Item.id).where(Item.batch_id == batch.id, Item.status.not_in(['approved', 'excluded'])).limit(1))
    batch.status = "in_review" if remaining else "completed"
    audit(db, user, "image_review." + action, item.id, ds.space_id, revision=item.revision)
    db.commit()
    return public_item(item)


@router.put("/items/{item_id}")
def edit(item_id: str, data: EditInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    item, batch, ds, permissions = locked_item_access(db, user, item_id, request.app.state.settings)
    require(permissions["can_edit"], "缺少标注编辑权限")
    require(item.status in {"pending", "submitted", "rejected"}, "当前状态不能修改", 409)
    annotation = checked_annotation(data.annotation.model_dump(), item.width, item.height,batch.provenance.get('review_template'))
    return change(db, user, item, batch, ds, data.revision, data.claim_token, "edit", data.note,
        # 一次保存即提交给下一位审核人，不再要求修改者额外点击“提交二次确认”。
        dict(annotation=annotation, editor_id=user.id, status="submitted", reviewer_id=None, reviewed_at=None))


@router.post("/items/{item_id}/transition")
def transition(item_id: str, data: Transition, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    item, batch, ds, permissions = locked_item_access(db, user, item_id, request.app.state.settings)
    if data.action == "submit":
        require(permissions["can_edit"])
        require(item.status in {"pending", "rejected"}, "当前状态不能提交", 409)
        # 提交人承担本次标注责任，即使原预测无需修改。
        values = dict(status="submitted", editor_id=user.id, reviewer_id=None, reviewed_at=None)
    else:
        require(permissions["can_review"], "缺少独立复核权限")
        require(item.status == "submitted", "只能复核已提交的标注", 409)
        require(item.editor_id != user.id and batch.creator_id != user.id, "不能复核自己创建或修改的标注")
        if data.action == "reject":
            require(data.note, "退回修改时请填写原因", 400)
        values = dict(status="approved" if data.action == "approve" else "rejected",
                      reviewer_id=user.id, reviewed_at=now())
    return change(db, user, item, batch, ds, data.revision, data.claim_token, data.action, data.note, values)


@router.get("/items/{item_id}/history")
def history(item_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    item_access(db, user, item_id, request.app.state.settings)
    return [public_row(e) for e in db.scalars(select(Event).where(Event.item_id == item_id).order_by(Event.revision))]


@router.get("/{batch_id}/export")
def export(batch_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    batch = fetch(db, Batch, batch_id)
    version, ds, _ = access(db, user, batch, request.app.state.settings)
    rows = item_rows(db, batch)
    approved = [i for i in rows if i.status == "approved"]
    pages=db.scalars(select(ReviewImage).where(ReviewImage.batch_id==batch.id)).all()
    image_complete=bool(pages) and all(p.status=='approved' for p in pages)
    require(approved or image_complete, '没有已复核标注或完整确认的负样本，不能导出',409)
    annotations = []
    for item in approved:
        asset = fetch(db, Asset, item.asset_id)
        require(asset.status == "clean" and asset.version_id == version.id, "原始图像不可用", 409)
        annotations.append({**public_item(item), "filename": asset.filename, "sha256": asset.sha256})
    result = {"schema_version": 2, "kind": "approved_person_annotations", "batch_id": batch.id,
              "source_version_id": version.id, "source": version.source, "license": version.license,
              "exported_at": now().isoformat() + "Z", "provenance": batch.provenance,
              "coordinate_system": "exif_transposed_pixels_xywh", "partial": any(i.status not in {"approved","excluded"} for i in rows),
              "excluded_unapproved": sum(i.status not in {"approved","excluded"} for i in rows),
              "excluded_non_person": sum(i.status=="excluded" for i in rows), "annotations": annotations,
              'image_complete':image_complete,'images':[{'asset_id':p.asset_id,'width':p.width,'height':p.height,
                  'status':p.status,'no_person':p.snapshot.get('no_person',False),
                  'dedup_excluded':p.snapshot.get('dedup_excluded',False),'duplicate_of':p.snapshot.get('duplicate_of')} for p in pages],
              "limitations": ([] if image_complete else ["整图完整性尚未确认，不是完整检测训练集"])+
                  ["审核覆盖范围仅为人物与本批次属性，不代表所有质量维度已通过",
                   "不包含图像文件；原图需使用资产 ID 在版本授权范围内下载"]}
    if batch.provenance.get('review_template'):
        result.update(kind='approved_object_annotations',review_template=batch.provenance['review_template'],
            template_digest=batch.provenance['template_digest'],excluded_non_target=result['excluded_non_person'])
        result['limitations']=[text.replace('人物','目标') for text in result['limitations']]
        for row in result['images']:row['no_target']=row['no_person']
    audit(db, user, "image_review.export", batch.id, ds.space_id, revisions={i.id: i.revision for i in approved})
    db.commit()
    # public_row 中的 datetime 经 FastAPI 编码再构造附件响应。
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="approved-annotations-{batch.id}.json"'})
