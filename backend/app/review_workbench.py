"""A version-wide image queue over worker shards; no review verdicts are automated."""
from fastapi import APIRouter,Depends,Request,Query
from sqlalchemy import select,func,case,or_,and_,exists
from .common import fetch,require
from .db import get_db,now
from .models import Dataset,Version,Membership,Asset,ImageReviewBatch as Batch,ImageReviewImage as Page,ImageReviewItem as Item
from .security import interactive_user
from .schemas import Input
from pydantic import Field
from .policy import member,manage_dataset
from .workflows import locked_version
from .image_reviews import access
from .image_completeness import claim_image

router=APIRouter(prefix='/review-workbench',tags=['整版连续图像审核'])

def scope(db,user,version_id):
    version=fetch(db,Version,version_id);ds=fetch(db,Dataset,version.dataset_id);role=member(db,user,ds.space_id)
    owner=manage_dataset(db,user,ds)
    from .joint_review import flags
    a,r=flags(db,user,version);staff=a or r
    submitted=db.scalar(select(Batch.id).where(Batch.version_id==version_id,Batch.status.not_in(['draft','discarded'])).limit(1)) if staff and not owner else None
    require(ds.status=='active' and (owner or staff and submitted),'审核版本不存在或没有权限',404)
    return version,ds,owner,staff and not owner

def base(version_id):
    return select(Page.id).join(Batch,Page.batch_id==Batch.id).where(Batch.version_id==version_id,Batch.status.not_in(['draft','discarded']))

def eligible(user,annotate=True,review=True):
    stamp=now()
    return and_(Batch.creator_id!=user.id,
        or_(and_(annotate,Page.status=='pending'),and_(review,Page.status=='submitted',Page.editor_id!=user.id)),
        or_(Page.claimed_by_id.is_(None),Page.claim_expires_at.is_(None),Page.claim_expires_at<=stamp),
        ~exists(select(Item.id).where(Item.batch_id==Page.batch_id,Item.asset_id==Page.asset_id,Item.claim_expires_at>stamp)))

@router.get('')
def inbox(db=Depends(get_db),user=Depends(interactive_user),limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0)):
    owner=Dataset.owner_id==user.id
    from .datasets import json_member
    legacy=and_(Version.card['_review_pipeline'].as_string().is_(None),Membership.role.in_(['admin','reviewer']))
    annotate=or_(legacy,json_member(db,Version.card['_review_pipeline']['annotators'],user.id))
    review=or_(legacy,and_(Membership.role.in_(['admin','reviewer']),json_member(db,Version.card['_review_pipeline']['reviewers'],user.id)))
    visible=or_(owner,and_(or_(annotate,review),Batch.status!='draft'))
    query=select(Version.id.label('version_id'),Version.number,Dataset.id.label('dataset_id'),Dataset.name,
        Dataset.owner_id,Membership.role,annotate.label('annotate_flag'),review.label('review_flag'),func.count(Page.id).label('images'),
        *[func.sum(case((and_(Batch.status!='draft',Page.status==state),1),else_=0)).label(state) for state in ['pending','submitted','approved']],
        func.sum(case((Batch.status=='draft',1),else_=0)).label('draft'))
    rows=db.execute(query.select_from(Version).join(Batch,Batch.version_id==Version.id).join(Page,Page.batch_id==Batch.id)
        .join(Dataset,Version.dataset_id==Dataset.id).join(Membership,and_(Membership.space_id==Dataset.space_id,Membership.user_id==user.id,Membership.active.is_(True)))
        .where(Dataset.status=='active',Batch.status!='discarded',visible)
        .group_by(Version.id,Version.number,Dataset.id,Dataset.name,Dataset.owner_id,Membership.role)
        .order_by(Version.id).offset(offset).limit(limit+1)).mappings().all()
    return {'items':[{**dict(r),'can_review':r['owner_id']!=user.id and bool(r['annotate_flag'] or r['review_flag']),
        'can_annotate':r['owner_id']!=user.id and bool(r['annotate_flag']),'can_approve':r['owner_id']!=user.id and bool(r['review_flag'])} for r in rows[:limit]],'has_more':len(rows)>limit}

@router.get('/batches/{batch_id}')
def batch_version(batch_id:str,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    batch=fetch(db,Batch,batch_id);access(db,user,batch,request.app.state.settings)
    return {'version_id':batch.version_id}

@router.get('/{version_id}')
def progress(version_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    version,ds,owner,staff=scope(db,user,version_id)
    from .joint_review import flags,config
    a,r=flags(db,user,version)
    counts=dict(db.execute(select(Page.status,func.count(Page.id)).join(Batch,Page.batch_id==Batch.id)
        .where(Batch.version_id==version_id,Batch.status.not_in(['draft','discarded'])).group_by(Page.status)).all())
    draft=db.scalar(select(func.count(Page.id)).join(Batch,Page.batch_id==Batch.id).where(Batch.version_id==version_id,Batch.status=='draft'))
    available=db.scalar(select(func.count()).select_from(base(version_id).where(eligible(user,a,r)).subquery())) if staff else 0
    return {'version_id':version.id,'dataset_id':ds.id,'dataset_name':ds.name,'number':version.number,
        'counts':counts,'draft_images':draft,'total_images':sum(counts.values()),'available':available,
        'can_manage':owner,'space_id':ds.space_id,'owner_id':ds.owner_id,'can_review':staff and version.status in {'draft','rejected'},
        'can_annotate':a,'can_approve':r,'assignment':config(version),'status':version.status}

class NextImage(Input):
    after_image_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')

@router.post('/{version_id}/next')
def claim_next(version_id:str,data:NextImage,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    locked_version(db,version_id)
    version,_,_,staff=scope(db,user,version_id)
    require(staff and version.status in {'draft','rejected'},'请使用独立审核人账号；负责人不能自审',403)
    from .joint_review import flags,config,order_candidates
    a,r=flags(db,user,version)
    # Repeated requests/tabs resume the same lease rather than holding multiple shards.
    held=db.scalar(base(version_id).where(Page.claimed_by_id==user.id,Page.claim_expires_at>now(),
        or_(and_(a,Page.status=='pending'),and_(r,Page.status=='submitted',Page.editor_id!=user.id))).order_by(Page.id).limit(1))
    query=base(version_id).where(eligible(user,a,r))
    identifier=held
    if not identifier:
        if data.after_image_id:
            previous=db.execute(select(Page.created_at,Page.id,Page.asset_id).join(Batch,Page.batch_id==Batch.id)
                .where(Page.id==data.after_image_id,Batch.version_id==version_id)).one_or_none()
            require(previous is not None,'上一张图片不属于此版本',404)
            if config(version) and config(version).get('report_id'):identifier=order_candidates(db,version,query,previous[2])
            else:identifier=db.scalar(query.where(or_(Page.created_at>previous[0],and_(Page.created_at==previous[0],Page.id>previous[1])))
                    .order_by(Page.created_at,Page.id).limit(1))
        if not identifier:
            wrapped=query.where(Page.id!=data.after_image_id) if data.after_image_id else query
            identifier=order_candidates(db,version,wrapped) if config(version) and config(version).get('report_id') else db.scalar(wrapped.order_by(Page.created_at,Page.id).limit(1))
    if not identifier:
        db.rollback();return {'image':None,'message':'本轮没有其他可领取图片；可能等待另一人确认、其他人领取或后续识别结果。跳过的图片仍在待办中。'}
    image=claim_image(identifier,request,db,user)
    batch=fetch(db,Batch,image['batch_id']);asset=fetch(db,Asset,image['asset_id'])
    return {'image':image,'batch':{'id':batch.id,'provenance':batch.provenance,'can_edit':a,'can_review':r,'joint_mode':bool(config(version)),
        'lease_seconds':request.app.state.settings.review_lease_seconds,'items':image['items'],
        'images':[{**{k:v for k,v in image.items() if k not in {'claim_token','items'}},'filename':asset.filename}]}}
