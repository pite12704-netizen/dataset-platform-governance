"""Version-local job separation and immutable-report-backed, per-pair review."""
import hashlib
from functools import lru_cache
from types import SimpleNamespace
from fastapi import APIRouter,Depends,Request
from pydantic import Field,model_validator
from sqlalchemy import select,func,or_,and_,update
from .common import fetch,require,audit
from .db import get_db
from .schemas import Input
from .models import Version,Dataset,Membership,User,Asset,GovernanceReport,DuplicatePairReview as Pair,ImageReviewImage as Page,ImageReviewBatch as Batch,Job
from .policy import member,manage_dataset
from .security import interactive_user
from .quality_contract import media_manifest
from .workflows import locked_version
from .concurrency import media_slot

router=APIRouter(prefix='/joint-review',tags=['标注与去重联合作业'])
def config(version):return version.card.get('_review_pipeline')
def flags(db,user,version):
    ds=fetch(db,Dataset,version.dataset_id);m=member(db,user,ds.space_id);c=config(version)
    if not m or ds.status!='active' or ds.owner_id==user.id:return False,False
    if c:return user.id in c['annotators'],user.id in c['reviewers'] and m.role in {'admin','reviewer'}
    return m.role in {'admin','reviewer'},m.role in {'admin','reviewer'}
def images(db,version_id):
    rows=db.execute(select(Asset.id,Asset.filename,Asset.size,Asset.sha256).where(Asset.version_id==version_id)).mappings()
    return [SimpleNamespace(**r) for r in rows if r['filename'].lower().endswith(('.jpg','.jpeg','.png','.webp'))]
def bound(db,version):
    c=config(version)
    if not c or not c.get('report_id'):return None
    report=fetch(db,GovernanceReport,c['report_id'])
    require(report.version_id==version.id and report.kind=='dedup' and report.status in {'pending','approved'},'关联去重报告已失效',409)
    return report

class Assignment(Input):
    annotators:list[str]=Field(min_length=1,max_length=50)
    reviewers:list[str]=Field(min_length=1,max_length=50)
    report_id:str|None=None
    @model_validator(mode='after')
    def separate(self):
        require(not set(self.annotators)&set(self.reviewers),'标注账号与审核账号必须不同',422)
        require(len(set(self.annotators))==len(self.annotators) and len(set(self.reviewers))==len(self.reviewers),'账号不能重复',422)
        return self

@router.get('/versions/{version_id}')
def read_assignment(version_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    v=fetch(db,Version,version_id);ds=fetch(db,Dataset,v.dataset_id);a,r=flags(db,user,v)
    require(manage_dataset(db,user,ds) or a or r,'没有此版本作业权限',403)
    return {'assignment':config(v),'can_annotate':a,'can_review':r}

@router.get('/versions/{version_id}/reports')
def report_options(version_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    v=fetch(db,Version,version_id);require(manage_dataset(db,user,fetch(db,Dataset,v.dataset_id)),'仅负责人可配置',403)
    length=func.json_length if db.bind.dialect.name=='mysql' else func.json_array_length
    rows=db.execute(select(GovernanceReport.id,GovernanceReport.status,GovernanceReport.summary['images'].label('images'),
        length(GovernanceReport.summary['pairs']).label('pairs')).where(GovernanceReport.version_id==version_id,
        GovernanceReport.kind=='dedup',GovernanceReport.status.in_(['pending','approved']))
        .order_by(GovernanceReport.created_at.desc(),GovernanceReport.id.desc()).limit(50)).mappings().all()
    return {'items':[dict(r) for r in rows]}

@router.put('/versions/{version_id}')
def assign(version_id:str,data:Assignment,db=Depends(get_db),user=Depends(interactive_user)):
    v,ds=locked_version(db,version_id);require(manage_dataset(db,user,ds),'仅负责人可分配岗位')
    require(v.status in {'draft','rejected'},'版本已冻结',409)
    for identifier in data.annotators+data.reviewers:
        u=fetch(db,User,identifier);m=member(db,u,ds.space_id)
        require(u.active and m and identifier!=ds.owner_id,'岗位账号须为有效组织成员，且不能是负责人',422)
        if identifier in data.reviewers:require(m.role in {'admin','reviewer'},'终审账号须具备组织审核权限',422)
    old=config(v)
    require(not db.scalar(select(Page.asset_id).join(Batch,Page.batch_id==Batch.id)
        .where(Batch.version_id==v.id,Batch.status.not_in(['draft','discarded']))
        .group_by(Page.asset_id).having(func.count()>1).limit(1)),'同一图片存在多个送审批次，请先处置重复候选或在新版本作业',409)
    if old and old.get('report_id')!=data.report_id:
        edited=db.scalar(select(Page.id).join(Batch,Page.batch_id==Batch.id).where(Batch.version_id==v.id,Page.editor_id.is_not(None)).limit(1))
        require(not edited,'已有联合作业记录，切换报告需新版本',409)
    from .imports import require_complete
    require_complete(db,v.id)
    from .models import Upload
    require(not db.scalar(select(Upload.id).where(Upload.version_id==v.id,Upload.status.in_(['uploading','finalizing','aborting'])).limit(1)),'请先完成或取消上传',409)
    population=images(db,v.id);fingerprint=media_manifest(population)
    require(population and not db.scalar(select(Asset.id).where(Asset.version_id==v.id,Asset.status!='clean').limit(1)),
        '请先完成本版本全部文件检查，且至少包含一张图片',409)
    if data.report_id:
        report=fetch(db,GovernanceReport,data.report_id)
        require(report.version_id==v.id and report.kind=='dedup' and report.status in {'pending','approved'} and report.summary.get('media_manifest')==fingerprint,'请选择当前图片清单的有效去重报告',409)
        require(report.status!='approved' or report.reviewer_id not in data.annotators,'已审批该报告的账号不能再担当本版标注员，请重新分配岗位或生成新报告',409)
        if report.status=='approved':consistent(report,set(report.decision.get('duplicate_pairs',[])))
        existing={p.pair_id for p in db.scalars(select(Pair).where(Pair.report_id==report.id))}
        accepted=set(report.decision.get('duplicate_pairs',[])) if report.status=='approved' else None
        for p in report.summary['pairs']:
            if p['id'] not in existing:
                db.add(Pair(id=hashlib.sha256((report.id+p['id']).encode()).hexdigest()[:32],report_id=report.id,pair_id=p['id'],
                    left_asset_id=p['left'],right_asset_id=p['right'],revision=0,
                    approved_duplicate=p['id'] in accepted if accepted is not None else None,
                    reviewer_id=report.reviewer_id if accepted is not None else None))
    c={**data.model_dump(),'media_manifest':fingerprint}
    batch_ids=select(Batch.id).where(Batch.version_id==v.id)
    db.execute(update(Page).where(Page.batch_id.in_(batch_ids),Page.claim_token.is_not(None)).values(claim_token=None,claimed_by_id=None,claim_expires_at=None))
    from .models import ImageReviewItem
    db.execute(update(ImageReviewItem).where(ImageReviewItem.batch_id.in_(batch_ids),ImageReviewItem.claim_token.is_not(None))
        .values(claim_token=None,claimed_by_id=None,claim_expires_at=None))
    v.card={**v.card,'_review_pipeline':c};audit(db,user,'joint_review.assign',v.id,ds.space_id,before=old,after=c)
    db.commit();return {'assignment':c}

@lru_cache(maxsize=16)
def grouping(report_id,manifest,edges):
    parent={}
    def root(x):
        parent.setdefault(x,x)
        while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
        return x
    for left,right in edges:
        a,b=root(left),root(right)
        if a!=b:parent[max(a,b)]=min(a,b)
    return {key:root(key) for key in parent}

def consistent(report,duplicates):
    pairs=report.summary['pairs']
    roots=grouping(report.id,report.summary['media_manifest']+'-confirmed',tuple((p['left'],p['right']) for p in pairs if p['id'] in duplicates))
    require(not any(p['id'] not in duplicates and roots.get(p['left'],p['left'])==roots.get(p['right'],p['right']) for p in pairs),
        '重复关系存在矛盾：已判不同的图片被其他重复边连在一起。请退回补正，不能自动合并',409)

def order_candidates(db,version,query,after_asset=None):
    report=bound(db,version)
    if not report:return None
    pairs=report.summary['pairs'];groups=grouping(report.id,report.summary['media_manifest'],tuple((p['left'],p['right']) for p in pairs))
    rows=db.execute(query.with_only_columns(Page.id,Page.asset_id)).all()
    rows=sorted(rows,key=lambda r:(groups.get(r.asset_id,r.asset_id),r.asset_id,r.id))
    if after_asset:
        later=[r for r in rows if (groups.get(r.asset_id,r.asset_id),r.asset_id)>(groups.get(after_asset,after_asset),after_asset)]
        if later:return later[0].id
        rows=[r for r in rows if r.asset_id!=after_asset]
    return rows[0].id if rows else None

class PairProposal(Input):
    pair_id:str=Field(min_length=24,max_length=24)
    duplicate:bool

def proposals(db,version,page,user,values,excluded):
    report=bound(db,version)
    if not report:
        require(not values and not excluded,'未关联去重报告',422);return {}
    rows=list(db.scalars(select(Pair).where(Pair.report_id==report.id,Pair.right_asset_id==page.asset_id).with_for_update()))
    mapped={v.pair_id:v.duplicate for v in values}
    require(len(mapped)==len(values) and set(mapped)<={r.pair_id for r in rows},'重复判定引用无效或跨图片',422)
    kinds={p['id']:p['kind'] for p in report.summary['pairs']}
    approved=set(report.decision.get('duplicate_pairs',[])) if report.status=='approved' else None
    for row in rows:
        value=(row.pair_id in approved) if approved is not None else True if kinds[row.pair_id]=='exact' else mapped.get(row.pair_id)
        require(value is not None,'请先完成本图全部重复候选初判',409)
        require(row.pair_id not in mapped or mapped[row.pair_id]==value,'不能更改已确认或SHA确定的重复关系',409)
        require(row.reviewer_id is None or row.approved_duplicate==value,'已确认的去重关系不能原地改变，请新建版本',409)
        if row.reviewer_id is None:row.proposed_duplicate=value;row.annotator_id=user.id;row.revision+=1
    duplicates=[r.left_asset_id for r in rows if r.approved_duplicate and r.reviewer_id] + [r.left_asset_id for r in rows if not r.reviewer_id and r.proposed_duplicate]
    require(not excluded or duplicates,'只有明确建议重复的图片才能跳过框选',422)
    audit(db,user,'joint_review.propose',page.id,version_id=version.id,report_id=report.id,
        decisions=[{'pair_id':r.pair_id,'duplicate':r.proposed_duplicate,'revision':r.revision} for r in rows])
    return {'dedup_report_id':report.id,'dedup_excluded':excluded,'duplicate_of':min(duplicates) if excluded else None}

def confirm_pairs(db,version,page,user):
    report=bound(db,version)
    if not report:return
    rows=list(db.scalars(select(Pair).where(Pair.report_id==report.id,Pair.right_asset_id==page.asset_id).with_for_update()))
    require(not rows or page.snapshot.get('dedup_report_id')==report.id,'此图缺少联合作业初判，请退回标注补充',409)
    if not rows and page.snapshot.get('dedup_report_id')!=report.id:
        page.snapshot={**page.snapshot,'dedup_report_id':report.id,'dedup_excluded':False}
    for row in rows:
        if row.reviewer_id:
            require(row.approved_duplicate is not None,'已审核关系不完整',409);continue
        require(row.annotator_id and row.annotator_id!=user.id and row.proposed_duplicate is not None,'重复关系须由标注员初判、另一账号终审',409)
        if report.status=='approved':require(row.proposed_duplicate==(row.pair_id in set(report.decision['duplicate_pairs'])),'初判与已批准报告不一致，请退回补正',409)
        if row.reviewer_id:require(row.approved_duplicate==row.proposed_duplicate,'重复关系确认冲突',409)
        row.approved_duplicate=row.proposed_duplicate;row.reviewer_id=user.id;row.revision+=1
    audit(db,user,'joint_review.confirm_pairs',page.id,version_id=version.id,report_id=report.id,
        decisions=[{'pair_id':r.pair_id,'duplicate':r.approved_duplicate,'reviewer_id':r.reviewer_id,'revision':r.revision} for r in rows])
    db.flush()
    if report.status=='pending' and not db.scalar(select(func.count()).select_from(Pair).where(Pair.report_id==report.id,Pair.reviewer_id.is_(None))):
        all_rows=list(db.scalars(select(Pair).where(Pair.report_id==report.id)))
        require({r.pair_id for r in all_rows}=={p['id'] for p in report.summary['pairs']},'去重候选未完整登记',409)
        consistent(report,{r.pair_id for r in all_rows if r.approved_duplicate})
        report.status='approved';report.reviewer_id=user.id
        report.decision={'reviewed_pairs':[r.pair_id for r in all_rows], 'duplicate_pairs':[r.pair_id for r in all_rows if r.approved_duplicate],
            'method':'annotation-and-independent-pair-review-v1','reviewer_ids':sorted({r.reviewer_id for r in all_rows}|{user.id})}
        audit(db,user,'joint_review.dedup_approved',report.id,operation='all_pairs_independently_confirmed')

@router.get('/images/{page_id}/similar')
def similar(page_id:str,db=Depends(get_db),user=Depends(interactive_user)):
    page=fetch(db,Page,page_id);batch=fetch(db,Batch,page.batch_id);v=fetch(db,Version,batch.version_id)
    a,r=flags(db,user,v);ds=fetch(db,Dataset,v.dataset_id);require(a or r or manage_dataset(db,user,ds),'没有作业权限',403)
    report=bound(db,v)
    if not report:return {'pairs':[],'report_id':None}
    by_id={p['id']:p for p in report.summary['pairs']}
    rows=list(db.scalars(select(Pair).where(Pair.report_id==report.id,or_(Pair.right_asset_id==page.asset_id,Pair.left_asset_id==page.asset_id))))
    accepted=set(report.decision.get('duplicate_pairs',[])) if report.status=='approved' else None
    return {'report_id':report.id,'images':report.summary['images'],'pair_count':len(report.summary['pairs']),'method':report.summary['method'],
        'requires_resubmit':page.status=='submitted' and any(p.right_asset_id==page.asset_id for p in rows) and page.snapshot.get('dedup_report_id')!=report.id,
        'pairs':[{**by_id[p.pair_id],'owned':p.right_asset_id==page.asset_id,'reference_asset_id':p.left_asset_id if p.right_asset_id==page.asset_id else p.right_asset_id,
        'proposed':p.proposed_duplicate,'approved':p.pair_id in accepted if accepted is not None else p.approved_duplicate,'confirmed':accepted is not None or bool(p.reviewer_id)} for p in rows],
        'strategy':'稳定资产ID代表；近重复不复用框，原图保留'}

@router.get('/images/{page_id}/reference/{asset_id}/preview',dependencies=[Depends(media_slot)])
def reference_preview(page_id:str,asset_id:str,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    import io,base64
    from .image_reviews import read_image
    page=fetch(db,Page,page_id);batch=fetch(db,Batch,page.batch_id);v=fetch(db,Version,batch.version_id)
    a,r=flags(db,user,v);ds=fetch(db,Dataset,v.dataset_id);require(a or r or manage_dataset(db,user,ds),'没有作业权限',403)
    report=bound(db,v);require(report is not None,'没有关联去重报告',404)
    require(db.scalar(select(Pair.id).where(Pair.report_id==report.id,or_(and_(Pair.right_asset_id==page.asset_id,Pair.left_asset_id==asset_id),
        and_(Pair.left_asset_id==page.asset_id,Pair.right_asset_id==asset_id))).limit(1)),'不是当前图的候选参考',404)
    asset=fetch(db,Asset,asset_id);require(asset.version_id==v.id,'候选不属于版本',404)
    image=read_image(asset,request.app.state.storage)
    try:image.thumbnail((640,640));out=io.BytesIO();image.save(out,'JPEG',quality=80)
    finally:image.close()
    return {'asset_id':asset.id,'content':'data:image/jpeg;base64,'+base64.b64encode(out.getvalue()).decode()}

def validate_backup(tables):
    """Reject cross-version or fabricated pair links before restoring metadata."""
    reports={r['id']:r for r in tables.get('governance_reports',[])}
    assets={a['id']:a for a in tables.get('assets',[])}
    users={u['id'] for u in tables.get('users',[])}
    candidates={};seen=set()
    for row in tables.get('duplicate_pair_reviews',[]):
        report=reports.get(row['report_id'])
        if not report or report['kind']!='dedup':raise ValueError('Pair review report is invalid')
        if report['id'] not in candidates:candidates[report['id']]={p['id']:(p['left'],p['right']) for p in report['summary']['pairs']}
        pair=(row['report_id'],row['pair_id'])
        if pair in seen or candidates[report['id']].get(row['pair_id'])!=(row['left_asset_id'],row['right_asset_id']):raise ValueError('Pair review endpoints or identity mismatch')
        seen.add(pair)
        if any(identifier not in assets or assets[identifier]['version_id']!=report['version_id'] for identifier in (row['left_asset_id'],row['right_asset_id'])):raise ValueError('Pair review crosses a version')
        if any(row.get(field) and row[field] not in users for field in ('annotator_id','reviewer_id')):raise ValueError('Pair actor is missing')
        if row.get('annotator_id') and row.get('annotator_id')==row.get('reviewer_id'):raise ValueError('Pair review self approval')
        if row.get('reviewer_id') and type(row.get('approved_duplicate')) is not bool:raise ValueError('Pair verdict is missing')
    datasets={d['id']:d for d in tables.get('datasets',[])}
    memberships={(m['space_id'],m['user_id']) for m in tables.get('memberships',[])}
    version_images={}
    for asset in assets.values():
        if asset['filename'].lower().endswith(('.jpg','.jpeg','.png','.webp')):version_images.setdefault(asset['version_id'],[]).append(SimpleNamespace(**asset))
    for version in tables.get('versions',[]):
        c=version.get('card',{}).get('_review_pipeline')
        if not c:continue
        assignment=Assignment.model_validate({k:c.get(k) for k in ('annotators','reviewers','report_id')})
        ds=datasets.get(version['dataset_id'])
        if not ds or any(u not in users or (ds['space_id'],u) not in memberships or u==ds['owner_id'] for u in assignment.annotators+assignment.reviewers):raise ValueError('Invalid version review assignment')
        fingerprint=media_manifest(version_images.get(version['id'],[]))
        if c.get('media_manifest')!=fingerprint:raise ValueError('Review assignment population changed')
        if assignment.report_id:
            report=reports.get(assignment.report_id)
            if not report or report['version_id']!=version['id']:raise ValueError('Assignment report crosses a version')
            if report['summary'].get('media_manifest')!=fingerprint:raise ValueError('Assignment report population changed')
            expected={(report['id'],p['id']) for p in report['summary']['pairs']}
            if not expected<=seen:raise ValueError('Assignment pair ledger is incomplete')
