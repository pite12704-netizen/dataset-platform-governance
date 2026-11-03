"""【学习标注：整图复核】领取整图 → 补漏/排除 → 第二人确认，所有保存带修订号。"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select

from .common import audit, fetch, public_row, require
from .db import get_db, now, uid
from .models import ImageReviewImage as Page, ImageReviewImageEvent as PageEvent
from .models import ImageReviewBatch as Batch, ImageReviewItem as Item, ImageReviewEvent as Event,Version
from .joint_review import PairProposal
from .schemas import Input
from .security import interactive_user
from .image_reviews import access, checked_annotation, public_item, assert_lease, Annotation
from .workflows import locked_version

router = APIRouter(prefix='/api/v1/image-reviews/images', tags=['整图完整性复核'])


class PersonEdit(Input):
    item_id: str | None = Field(default=None, max_length=32)
    annotation: Annotation


class SaveImage(Input):
    revision: int = Field(ge=0)
    claim_token: str = Field(pattern=r'^[a-f0-9]{32}$')
    persons: list[PersonEdit] = Field(max_length=200)
    duplicate_decisions:list[PairProposal]=Field(default_factory=list,max_length=20000)
    exclude_duplicate:bool=False


class ImageAction(Input):
    revision: int = Field(ge=0)
    claim_token: str = Field(pattern=r'^[a-f0-9]{32}$')


class ImageLease(Input):
    claim_token: str = Field(pattern=r'^[a-f0-9]{32}$')


class ReopenImage(Input):
    revision: int = Field(ge=0)


def locked_page(db, user, page_id, settings):
    batch_id = db.scalar(select(Page.batch_id).where(Page.id == page_id))
    require(batch_id, '整图记录不存在', 404)
    version_id = db.scalar(select(Batch.version_id).where(Batch.id == batch_id))
    locked_version(db, version_id)  # 与发布/人物保存统一 dataset → version → batch → page。
    batch = fetch(db, Batch, batch_id, lock=True)
    _, ds, permissions = access(db, user, batch, settings)
    require(permissions['can_review'] or permissions['can_edit'], '没有可编辑版本的作业权限')
    page = fetch(db, Page, page_id, lock=True)
    return page, batch, ds


def image_payload(db, page):
    return {**public_row(page, exclude=('claim_token',)),
        'items': [public_item(i) for i in db.scalars(select(Item).where(
            Item.batch_id == page.batch_id, Item.asset_id == page.asset_id).order_by(Item.person_key))]}


@router.get('/{page_id}/history')
def image_history(page_id: str,request: Request,db=Depends(get_db),user=Depends(interactive_user)):
    page=fetch(db,Page,page_id)
    access(db,user,fetch(db,Batch,page.batch_id),request.app.state.settings)
    return [public_row(e) for e in db.scalars(select(PageEvent).where(PageEvent.image_id==page.id).order_by(PageEvent.revision))]


@router.post('/{page_id}/claim')
def claim_image(page_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    page, batch, ds = locked_page(db, user, page_id, request.app.state.settings)
    from .joint_review import flags
    can_annotate,can_review=flags(db,user,fetch(db,Version,batch.version_id))
    require(can_annotate if page.status=='pending' else can_review,'当前账号不具备此阶段岗位权限',403)
    timestamp = now()
    require(page.status != 'approved', '整图已经确认完成', 409)
    require(page.status != 'submitted' or page.editor_id != user.id, '整图需另一位审核人确认', 409)
    own = page.claimed_by_id == user.id and page.claim_expires_at and page.claim_expires_at > timestamp
    require(own or not page.claim_expires_at or page.claim_expires_at <= timestamp, '整图已由其他审核人领取', 409)
    occupied = db.scalar(select(Item.id).where(Item.batch_id == batch.id, Item.asset_id == page.asset_id,
        Item.claim_expires_at > timestamp).limit(1))
    require(not occupied, '图片中的人物正在被审核，请等待领取释放', 409)
    other = db.scalar(select(Page.id).where(Page.batch_id == batch.id, Page.id != page.id,
        Page.claimed_by_id == user.id, Page.claim_expires_at > timestamp))
    require(not other, '请先释放当前领取的整图', 409)
    page.claimed_by_id, page.claim_token = user.id, page.claim_token if own else uid()
    page.claim_expires_at = timestamp + timedelta(seconds=request.app.state.settings.review_lease_seconds)
    audit(db, user, 'image_complete.claim', page.id, ds.space_id)
    db.commit()
    return {**image_payload(db, page), 'claim_token': page.claim_token}


def page_event(db, user, page, action, before):
    page.revision += 1
    db.add(PageEvent(image_id=page.id, actor_id=user.id, revision=page.revision, action=action,
        before=before, after={'status': page.status, 'snapshot': page.snapshot}))
    page.claimed_by_id, page.claim_token, page.claim_expires_at = None, None, None


@router.put('/{page_id}')
def save_image(page_id: str, data: SaveImage, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    page, batch, ds = locked_page(db, user, page_id, request.app.state.settings)
    from .joint_review import flags,config,proposals
    from .models import Version
    version=fetch(db,Version,batch.version_id);require(flags(db,user,version)[0],'标注岗位才能修改，审核岗位只能通过或退回',403)
    assert_lease(page, user, data.claim_token)
    require(page.revision == data.revision and page.status == 'pending', '整图状态或修订已变化', 409)
    before = {'status': page.status, 'snapshot': page.snapshot}
    combined=proposals(db,version,page,user,data.duplicate_decisions,data.exclude_duplicate) if config(version) else {}
    rows = {i.id: i for i in db.scalars(select(Item).where(Item.batch_id == batch.id, Item.asset_id == page.asset_id))}
    known = [p.item_id for p in data.persons if p.item_id]
    require(len(set(known)) == len(known) and set(known) <= rows.keys(), '人物引用重复或不属于本图', 422)
    active = []
    for person in ([] if data.exclude_duplicate else data.persons):
        annotation = checked_annotation(person.annotation.model_dump(), page.width, page.height,batch.provenance.get('review_template'))
        if person.item_id:
            item = rows[person.item_id]
            prior = {'annotation': item.annotation, 'status': item.status}
            if item.annotation == annotation and item.status == 'approved':
                active.append(item)
                continue
            item.annotation = annotation
        else:
            item = Item(batch_id=batch.id, asset_id=page.asset_id, person_key='human-' + uid(),
                width=page.width, height=page.height, predicted={}, annotation=annotation)
            db.add(item)
            db.flush()
            prior = {'annotation': None, 'status': 'absent'}
        item.status, item.editor_id, item.reviewer_id = 'submitted', user.id, None
        item.revision += 1
        db.add(Event(item_id=item.id, actor_id=user.id, revision=item.revision,
            action='image_edit', before=prior, after={'annotation': annotation, 'status': item.status}, note=''))
        active.append(item)
    for item in rows.values():
        if (data.exclude_duplicate or item.id not in known) and item.status != 'excluded':
            prior = {'annotation': item.annotation, 'status': item.status}
            item.status, item.editor_id, item.reviewer_id = 'excluded', user.id, None
            item.revision += 1
            db.add(Event(item_id=item.id, actor_id=user.id, revision=item.revision, action='exclude',
                before=prior, after={'annotation': item.annotation, 'status': 'excluded'}, note=''))
    db.flush()
    all_rows = db.scalars(select(Item).where(Item.batch_id == batch.id, Item.asset_id == page.asset_id)).all()
    page.status, page.editor_id, page.reviewer_id = 'submitted', user.id, None
    page.snapshot = {'items': [{'id': i.id, 'revision': i.revision} for i in all_rows],
                     'no_person': not active and not data.exclude_duplicate, 'complete': True,**combined}
    batch.status = 'in_review'
    page_event(db, user, page, 'submit', before)
    audit(db, user, 'image_complete.submit', page.id, ds.space_id, persons=len(active))
    db.commit()
    return image_payload(db, page)


@router.post('/{page_id}/approve')
def approve_image(page_id: str, data: ImageAction, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    page, batch, ds = locked_page(db, user, page_id, request.app.state.settings)
    from .joint_review import flags,config,confirm_pairs
    from .models import Version
    version=fetch(db,Version,batch.version_id);require(flags(db,user,version)[1],'审核岗位才能确认',403)
    assert_lease(page, user, data.claim_token)
    require(page.revision == data.revision and page.status == 'submitted', '整图尚未提交或已变化', 409)
    require(page.editor_id != user.id and batch.creator_id != user.id, '不能确认自己提交或修改的整图')
    if config(version):confirm_pairs(db,version,page,user)
    rows = db.scalars(select(Item).where(Item.batch_id == batch.id, Item.asset_id == page.asset_id)).all()
    actual = {i.id: i.revision for i in rows}
    require(actual == {i['id']: i['revision'] for i in page.snapshot['items']}, '人物标注已变化，请重新核对整图', 409)
    before = {'status': page.status, 'snapshot': page.snapshot}
    for item in rows:
        if item.status == 'approved':
            continue
        prior = {'annotation': item.annotation, 'status': item.status}
        item.status = 'excluded' if item.status == 'excluded' else 'approved'
        item.reviewer_id, item.reviewed_at = user.id, now()
        item.revision += 1
        db.add(Event(item_id=item.id, actor_id=user.id, revision=item.revision, action='image_approve',
            before=prior, after={'annotation': item.annotation, 'status': item.status}, note=''))
    page.status, page.reviewer_id = 'approved', user.id
    page.snapshot = {**page.snapshot, 'items': [{'id': i.id, 'revision': i.revision} for i in rows]}
    page_event(db, user, page, 'approve', before)
    db.flush()
    remaining = db.scalar(select(Page.id).where(Page.batch_id == batch.id, Page.status != 'approved').limit(1))
    people = db.scalar(select(Item.id).where(Item.batch_id == batch.id, Item.status.not_in(['approved', 'excluded'])).limit(1))
    batch.status = 'in_review' if remaining or people else 'completed'
    audit(db, user, 'image_complete.approve', page.id, ds.space_id)
    db.commit()
    return image_payload(db, page)


@router.post('/{page_id}/release')
def release_image(page_id: str, data: ImageLease, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    page, _, _ = locked_page(db, user, page_id, request.app.state.settings)
    if page.claimed_by_id == user.id and page.claim_token == data.claim_token:
        page.claimed_by_id, page.claim_token, page.claim_expires_at = None, None, None
        db.commit()
    return {'released': True}


@router.post('/{page_id}/reopen')
def reopen_image(page_id: str, data: ReopenImage, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    page, batch, ds = locked_page(db, user, page_id, request.app.state.settings)
    from .joint_review import config,flags
    from .models import Version
    version=fetch(db,Version,batch.version_id)
    if config(version):require(flags(db,user,version)[1],'只有审核岗位可以退回标注',403)
    require(page.revision == data.revision, '整图修订已变化', 409)
    require(not page.claim_expires_at or page.claim_expires_at <= now(), '请先释放有效领取', 409)
    before = {'status': page.status, 'snapshot': page.snapshot}
    if config(version) and config(version).get('report_id'):
        from .models import GovernanceReport,DuplicatePairReview
        from sqlalchemy import update
        report=fetch(db,GovernanceReport,config(version)['report_id'])
        if report.status=='pending':
            decisions=list(db.scalars(select(DuplicatePairReview).where(DuplicatePairReview.report_id==report.id,DuplicatePairReview.right_asset_id==page.asset_id)))
            audit(db,user,'joint_review.return_pairs',page.id,ds.space_id,
                before=[{'pair_id':r.pair_id,'proposal':r.proposed_duplicate,'approved':r.approved_duplicate,'annotator_id':r.annotator_id,'reviewer_id':r.reviewer_id,'revision':r.revision} for r in decisions])
            db.execute(update(DuplicatePairReview).where(DuplicatePairReview.report_id==report.id,DuplicatePairReview.right_asset_id==page.asset_id)
                .values(reviewer_id=None,approved_duplicate=None))
    page.status, page.snapshot, page.reviewer_id = 'pending', {}, None
    batch.status = 'in_review'
    page_event(db, user, page, 'reopen', before)
    audit(db, user, 'image_complete.reopen', page.id, ds.space_id)
    db.commit()
    return image_payload(db, page)


@router.post('/{page_id}/renew')
def renew_image(page_id: str, data: ImageLease, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    page, _, _ = locked_page(db, user, page_id, request.app.state.settings)
    assert_lease(page, user, data.claim_token)
    page.claim_expires_at = now() + timedelta(seconds=request.app.state.settings.review_lease_seconds)
    db.commit()
    return {'claim_expires_at': page.claim_expires_at}
