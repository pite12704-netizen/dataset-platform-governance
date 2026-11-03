"""Independent blind samples: custodian and approver are separate from producer."""
import hashlib
import json
import secrets
from fastapi import APIRouter,Depends,Request,Query
from pydantic import Field
from sqlalchemy import select
from .common import audit,fetch,public_row,require
from .db import get_db
from .models import Asset,Dataset,EvaluationSet,ImageReviewBatch,ImageReviewImage,Job,User,Version
from .policy import member,manage_dataset
from .quality_contract import annotation_snapshot,media_manifest
from .schemas import Input,JobInput
from .security import interactive_user
from .concurrency import media_slot

router=APIRouter(prefix='/api/v1/evaluation-sets',tags=['独立盲测'])


def access(db,user,item,producer_summary=False):
    version=fetch(db,Version,item.target_version_id);ds=fetch(db,Dataset,version.dataset_id)
    role=member(db,user,ds.space_id)
    require(ds.status=='active' and role,'盲测记录不存在',404)
    custodian=user.id in {item.creator_id,item.reviewer_id} and role.role=='evaluator' and user.id!=ds.owner_id
    require(custodian or producer_summary and user.id==ds.owner_id,'没有此盲测权限',403)
    return version,ds,bool(custodian)


def require_evaluator(db,user,set_id,version_id,mutable=True):
    item=fetch(db,EvaluationSet,set_id)
    version,ds,_=access(db,user,item)
    require(item.creator_id==user.id and version.id==version_id and item.status in {'ready','used'},'仅真值保管人可运行已就绪盲测',403)
    require(user.active and user.verified and not user.must_change_password and (not mutable or version.status in {'draft','rejected'}),'盲测账号或版本不可操作',409)
    truth=fetch(db,Asset,item.truth_asset_id);source=fetch(db,Dataset,fetch(db,Version,truth.version_id).dataset_id)
    require(source.visibility=='private' and manage_dataset(db,user,source) and truth.status=='clean' and truth.sha256==item.truth_sha256,
            '私有真值归属或内容已变化',409)
    snapshot=annotation_snapshot(db,version.id,complete=True)
    require(snapshot['hash']==item.annotation_manifest and media_manifest(snapshot['images'])==item.population_manifest,
            '生产标注或图片已变化，需重新独立抽样，不能针对旧答案反复调参',409)
    return item


class Create(Input):
    version_id:str=Field(min_length=32,max_length=32)
    reviewer_id:str=Field(min_length=32,max_length=32)
    sample_count:int=Field(ge=1,le=2000)


@router.post('',status_code=201)
def create(data:Create,db=Depends(get_db),user=Depends(interactive_user)):
    from .workflows import locked_version
    version,ds=locked_version(db,data.version_id)
    role=member(db,user,ds.space_id);reviewer=fetch(db,User,data.reviewer_id);other=member(db,reviewer,ds.space_id)
    require(role and role.role=='evaluator' and user.id!=ds.owner_id and not user.platform_admin,'抽样必须由独立测评员专用账号发起')
    require(other and other.role=='evaluator' and reviewer.active and reviewer.verified and not reviewer.platform_admin and
            reviewer.id not in {user.id,ds.owner_id},'需另一位独立审核人批准')
    from .models import ImageReviewItem
    for model in (ImageReviewItem,ImageReviewImage):
        participated=db.scalar(select(model.id).join(ImageReviewBatch,model.batch_id==ImageReviewBatch.id).where(
            ImageReviewBatch.version_id==version.id,(model.editor_id.in_([user.id,reviewer.id]))|
            (model.reviewer_id.in_([user.id,reviewer.id]))).limit(1))
        require(not participated,'盲测保管人与批准人不能曾参与此版本生产标注或复核',403)
    require(version.status in {'draft','rejected'},'盲测针对尚未发布的交付快照',409)
    snapshot=annotation_snapshot(db,version.id,complete=True)
    require(data.sample_count<=len(snapshot['images']),'样本数超过总体',422)
    ids=secrets.SystemRandom().sample(sorted(a.id for a in snapshot['images']),data.sample_count)
    item=EvaluationSet(target_version_id=version.id,creator_id=user.id,reviewer_id=reviewer.id,sample_ids=ids,
        population_manifest=media_manifest(snapshot['images']),annotation_manifest=snapshot['hash'])
    db.add(item);db.flush();audit(db,user,'evaluation_set.create',item.id,ds.space_id,sample_count=len(ids));db.commit()
    return public_row(item)


@router.get('')
def listing(db=Depends(get_db),user=Depends(interactive_user),limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0)):
    rows=db.scalars(select(EvaluationSet).where((EvaluationSet.creator_id==user.id)|(EvaluationSet.reviewer_id==user.id))
        .order_by(EvaluationSet.created_at.desc(),EvaluationSet.id.desc()).offset(offset).limit(limit+1)).all()
    items=[]
    for item in rows:
        try:access(db,user,item)
        except Exception:continue
        items.append(public_row(item))
    return {'items':items[:limit],'has_more':len(rows)>limit}


@router.get('/{set_id}')
def detail(set_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    item=fetch(db,EvaluationSet,set_id);version,ds,custodian=access(db,user,item,True)
    if not custodian:return {'id':item.id,'target_version_id':version.id,'status':item.status,'sample_count':len(item.sample_ids),'blind':True}
    pages=db.scalars(select(ImageReviewImage).where(ImageReviewImage.asset_id.in_(item.sample_ids),ImageReviewImage.status=='approved')).all()
    assets={a.id:a for a in db.scalars(select(Asset).where(Asset.id.in_(item.sample_ids)))}
    return {**public_row(item),'samples':[{'asset_id':p.asset_id,'filename':assets[p.asset_id].filename,'width':p.width,'height':p.height}
        for p in pages],'template':{'schema_version':1,'images':[{'asset_id':identifier,'persons':[]} for identifier in item.sample_ids]}}


@router.post('/{set_id}/revoke')
def revoke(set_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    from .workflows import locked_version
    from .models import Grant
    from .db import now
    item=fetch(db,EvaluationSet,set_id);version,ds=locked_version(db,item.target_version_id)
    access(db,user,item);item=fetch(db,EvaluationSet,item.id,lock=True)
    item.status='revoked'
    if version.status=='published' and version.quality_tier=='certified':
        version.status='withdrawn'
        for grant in db.scalars(select(Grant).where(Grant.version_id==version.id,Grant.revoked_at.is_(None)).with_for_update()):grant.revoked_at=now()
    audit(db,user,'evaluation_set.revoke',item.id,ds.space_id,version_status=version.status);db.commit()
    return {'status':item.status,'version_status':version.status}


class Truth(Input):
    truth_asset_id:str=Field(min_length=32,max_length=32)


@router.post('/{set_id}/truth')
def link_truth(set_id:str,data:Truth,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    item=fetch(db,EvaluationSet,set_id)
    from .workflows import locked_version
    version,ds=locked_version(db,item.target_version_id);access(db,user,item)
    item=fetch(db,EvaluationSet,item.id,lock=True)
    require(user.id==item.creator_id and item.status=='draft','只有保管人能配置未就绪真值',409)
    asset=fetch(db,Asset,data.truth_asset_id);truth_version=fetch(db,Version,asset.version_id);source=fetch(db,Dataset,truth_version.dataset_id)
    require(asset.version_id!=version.id and source.visibility=='private' and manage_dataset(db,user,source),
            '真值必须位于保管人拥有的独立私有版本，不能混入生产版本',403)
    require(asset.status=='clean' and 0<asset.size<=1024**2,'真值须已检查，最大1MiB',422)
    raw=b''.join(request.app.state.storage.chunks(asset.object_key,0,asset.size-1))
    require(hashlib.sha256(raw).hexdigest()==asset.sha256,'真值哈希不匹配',409)
    payload=json.loads(raw)
    rows=payload.get('images',[])
    require(payload.get('schema_version')==1 and len(rows)==len(item.sample_ids) and
        {row.get('asset_id') for row in rows}==set(item.sample_ids),'真值必须准确覆盖独立抽样清单，不能自选样本',422)
    from .image_reviews import checked_annotation
    pages={p.asset_id:p for p in db.scalars(select(ImageReviewImage).where(ImageReviewImage.asset_id.in_(item.sample_ids),ImageReviewImage.status=='approved'))}
    for row in rows:
        require(isinstance(row.get('persons'),list) and len(row['persons'])<=200,'真值人物结构无效',422)
        for person in row['persons']:checked_annotation(person,pages[row['asset_id']].width,pages[row['asset_id']].height)
    item.truth_asset_id=asset.id;item.truth_sha256=asset.sha256;item.status='ready'
    audit(db,user,'evaluation_set.truth.link',item.id,ds.space_id);db.commit()
    return {'id':item.id,'status':item.status}


@router.post('/{set_id}/run',status_code=201)
def run(set_id:str,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    item=fetch(db,EvaluationSet,set_id)
    from .workflows import locked_version
    version,ds=locked_version(db,item.target_version_id);require_evaluator(db,user,item.id,version.id)
    batch=db.scalar(select(ImageReviewBatch).where(ImageReviewBatch.version_id==version.id,ImageReviewBatch.status=='completed').order_by(ImageReviewBatch.id))
    from .tasks import queue_job,dispatch
    prior=db.scalar(select(Job).where(Job.user_id==user.id,Job.idempotency_key=='blind:'+item.id))
    if prior:
        from .quality_contract import publication_policy
        require(prior.parameters.get('governance_options',{}).get('publication_policy')==publication_policy(db,version,request.app.state.settings),
                '组织验收策略已变化，请建立新的独立盲测',409)
    job=queue_job(db,user,JobInput(version_id=version.id,kind='evaluate',evaluation_target='approved_annotations',
        truth_asset_id=item.truth_asset_id,batch_id=batch.id,evaluation_set_id=item.id,idempotency_key='blind:'+item.id),request.app.state.settings)
    item.status='used';db.commit();dispatch(request.app,job.id);db.refresh(job)
    return public_row(job)


@router.get('/{set_id}/assets/{asset_id}/preview',dependencies=[Depends(media_slot)])
def sample(set_id:str,asset_id:str,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    from .preview_cache import thumbnail
    from fastapi.responses import Response
    item=fetch(db,EvaluationSet,set_id);_,ds,_=access(db,user,item)
    require(asset_id in item.sample_ids,'不属于抽样清单',404)
    asset=fetch(db,Asset,asset_id)
    audit(db,user,'evaluation_set.preview',asset.id,ds.space_id,set_id=item.id);db.commit()
    return Response(thumbnail(request,asset,(1280,1280),85),media_type='image/jpeg',headers={'Cache-Control':'no-store'})
