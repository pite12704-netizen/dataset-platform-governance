"""Explicit responsibility handoff; managing members alone never grants data access."""
from fastapi import APIRouter, Depends, Request, Query
from pydantic import Field
from sqlalchemy import select

from .common import audit, fetch, notify, public_row, require
from .db import get_db
from .models import Dataset, Job, Membership, Upload, User, Version
from .policy import member, space_admin
from .schemas import Input
from .security import interactive_user, verify_password

router=APIRouter(prefix='/api/v1',tags=['数据负责人交接'])


class Transfer(Input):
    target_user_id:str=Field(min_length=32,max_length=32)
    reason:str=Field(min_length=5,max_length=2000)
    current_password:str=Field(min_length=1,max_length=128)


@router.get('/spaces/{space_id}/responsibilities')
def responsibilities(space_id:str,db=Depends(get_db),user=Depends(interactive_user),
                     limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000)):
    require(member(db,user,space_id),'没有空间管理范围')
    admin=space_admin(db,user,space_id)
    query=select(Dataset,User,Membership).join(User,Dataset.owner_id==User.id).outerjoin(Membership,
        (Membership.space_id==Dataset.space_id)&(Membership.user_id==Dataset.owner_id)).where(Dataset.space_id==space_id)
    if not admin:query=query.where(Dataset.owner_id==user.id)
    rows=db.execute(query.order_by(Dataset.id).offset(offset).limit(limit+1)).all()
    return {'items':[{'id':ds.id,'name':ds.name,'owner_id':owner.id,'owner_name':owner.name,
        'owner_available':bool(owner.active and membership and membership.active),
        'can_transfer':ds.owner_id==user.id,'can_recover':bool(user.platform_admin and ds.owner_id!=user.id and
            not (owner.active and membership and membership.active))} for ds,owner,membership in rows[:limit]],
        'has_more':len(rows)>limit}


@router.post('/datasets/{dataset_id}/transfer-owner')
def transfer_owner(dataset_id:str,data:Transfer,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    ds=fetch(db,Dataset,dataset_id,lock=True)
    original=fetch(db,User,ds.owner_id)
    original_member=member(db,original,ds.space_id)
    owner_available=original.active and original_member
    normal=ds.owner_id==user.id and member(db,user,ds.space_id)
    emergency=(not owner_available and user.platform_admin and user.id!=data.target_user_id and
               space_admin(db,user,ds.space_id) and request.state.mfa)
    require(normal or emergency,'正常转交需当前负责人；离职接管需另一位已启用 MFA 的平台及组织管理员')
    require(verify_password(data.current_password,user.password_hash),'当前密码不正确',401)
    target=fetch(db,User,data.target_user_id)
    membership=member(db,target,ds.space_id)
    require(target.id!=ds.owner_id and target.active and target.verified and not target.must_change_password and
            membership and membership.role in {'admin','contributor'},'新负责人须为当前组织有效管理员或贡献者',409)
    versions=db.scalars(select(Version).where(Version.dataset_id==ds.id).order_by(Version.id).with_for_update()).all()
    ids=[v.id for v in versions]
    require(not any(v.status=='pending_review' for v in versions),'请先处理待发布审核，再转交',409)
    require(not db.scalar(select(Job.id).where(Job.version_id.in_(ids),Job.status.in_({'queued','running'})).limit(1)) and
            not db.scalar(select(Upload.id).where(Upload.version_id.in_(ids),Upload.status.in_({'uploading','finalizing','aborting'})).limit(1)),
            '请先停止或完成上传与处理任务，再转交',409)
    from .imports import require_complete
    for version in versions:require_complete(db,version.id)
    ds.owner_id=target.id
    audit(db,user,'dataset.owner.transfer',ds.id,ds.space_id,previous_owner=original.id,new_owner=target.id,
          reason=data.reason,emergency=bool(emergency))
    notify(db,original.id,'数据负责人已转交',ds.name)
    notify(db,target.id,'你已成为数据负责人',ds.name)
    db.commit()
    return {'dataset_id':ds.id,'owner_id':target.id}


class Description(Input):
    name:str=Field(min_length=1,max_length=200)
    description:str=Field(default='',max_length=20000)
    category:str=Field(default='通用',max_length=80)
    tags:list[str]=Field(default_factory=list,max_length=20)


@router.put('/datasets/{dataset_id}/description')
def description(dataset_id:str,data:Description,db=Depends(get_db),user=Depends(interactive_user)):
    from .policy import manage_dataset
    ds=fetch(db,Dataset,dataset_id,lock=True)
    require(manage_dataset(db,user,ds) and ds.status=='active')
    for key,value in data.model_dump().items():setattr(ds,key,value)
    audit(db,user,'dataset.description.edit',ds.id,ds.space_id)
    db.commit()
    return public_row(ds)
