"""Organization ownership: current owner requests, successor independently accepts."""
from datetime import timedelta
from fastapi import APIRouter,Depends,Request
from pydantic import Field
from sqlalchemy import select
from .auth import sensitive_user
from .capacity import account_provision
from .common import audit,fetch,notify,public_row,require
from .db import get_db,now
from .models import Space,SpaceTransfer,User
from .policy import member,space_admin
from .schemas import SensitiveAccount
from .security import interactive_user

router=APIRouter(prefix='/api/v1/spaces',tags=['组织所有权交接'])


class Transfer(SensitiveAccount):
    target_user_id:str=Field(min_length=32,max_length=32)
    reason:str=Field(min_length=5,max_length=2000)


def participants(db,*identifiers):
    for identifier in sorted(set(identifiers)):fetch(db,User,identifier,lock=True)


@router.get('/{space_id}/ownership-transfers')
def listing(space_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    require(space_admin(db,user,space_id))
    return [public_row(row) for row in db.scalars(select(SpaceTransfer).where(SpaceTransfer.space_id==space_id)
        .order_by(SpaceTransfer.created_at.desc()).limit(50))]


@router.post('/{space_id}/ownership-transfers',status_code=201)
def create(space_id:str,data:Transfer,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    participants(db,user.id,data.target_user_id)
    user=sensitive_user(db,user,data,request.app.state.settings,request)
    space=fetch(db,Space,space_id,lock=True);target=fetch(db,User,data.target_user_id)
    require(space.kind=='organization' and space.owner_id==user.id and space_admin(db,user,space.id))
    require(target.id!=user.id and target.active and target.verified and not target.must_change_password and
            space_admin(db,target,space.id),'继任者须为另一位有效组织管理员',409)
    account_provision(db,target.id,space.quota_bytes,request.app.state.settings,organization=True)
    for row in db.scalars(select(SpaceTransfer).where(SpaceTransfer.space_id==space.id,SpaceTransfer.status=='pending').with_for_update()):
        row.status='cancelled';row.decided_at=now()
    transfer=SpaceTransfer(space_id=space.id,requester_id=user.id,target_user_id=target.id,
        reason=data.reason,expires_at=now()+timedelta(hours=24))
    db.add(transfer);db.flush();audit(db,user,'space.owner.request',transfer.id,space.id,target_user_id=target.id)
    notify(db,target.id,'请确认组织所有权交接',space.name);db.commit()
    return public_row(transfer)


@router.post('/{space_id}/ownership-transfers/{transfer_id}/accept')
def accept(space_id:str,transfer_id:str,data:SensitiveAccount,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    transfer=fetch(db,SpaceTransfer,transfer_id)
    require(transfer.space_id==space_id and transfer.target_user_id==user.id,'交接不存在',404)
    participants(db,transfer.requester_id,user.id)
    user=sensitive_user(db,user,data,request.app.state.settings,request)
    space=fetch(db,Space,space_id,lock=True);transfer=fetch(db,SpaceTransfer,transfer_id,lock=True)
    require(transfer.status=='pending' and transfer.expires_at>now() and space.owner_id==transfer.requester_id,
            '交接已处理、过期或原所有者已变化',409)
    require(space_admin(db,user,space.id),'继任者已失去管理员资格',403)
    previous=fetch(db,User,transfer.requester_id)
    require(previous.active and member(db,previous,space.id),'原所有者已失效，需先完成受控账号恢复',409)
    account_provision(db,user.id,space.quota_bytes,request.app.state.settings,organization=True)
    space.owner_id=user.id;transfer.status='accepted';transfer.decided_at=now()
    audit(db,user,'space.owner.accept',transfer.id,space.id,previous_owner=previous.id,new_owner=user.id)
    notify(db,previous.id,'组织所有权交接完成',space.name);db.commit()
    return public_row(transfer)


@router.post('/{space_id}/ownership-transfers/{transfer_id}/cancel')
def cancel(space_id:str,transfer_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    space=fetch(db,Space,space_id,lock=True);transfer=fetch(db,SpaceTransfer,transfer_id,lock=True)
    require(transfer.space_id==space.id and user.id in {space.owner_id,transfer.target_user_id})
    require(transfer.status=='pending','交接已处理',409)
    transfer.status='cancelled';transfer.decided_at=now()
    audit(db,user,'space.owner.cancel',transfer.id,space.id);db.commit()
    return public_row(transfer)
