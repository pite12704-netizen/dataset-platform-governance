"""【学习标注：数据治理】去重建议、固定分组划分、独立真值评测；任务不删除原图。"""
import hashlib
import json
import random

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import and_, case, or_, select

from .common import audit, fetch, public_row, require
from .db import get_db
from .models import Asset, Dataset, GovernanceReport, ImageReviewBatch, ImageReviewItem, ImageReviewImage, Job, Membership, Version
from .policy import can_read_content, member, manage_dataset
from .security import interactive_user
from .concurrency import media_slot
from .schemas import Input
from .image_reviews import Annotation, read_image
from .vision import image_assets

router = APIRouter(prefix='/api/v1/governance', tags=['数据治理与评测'])


def manifest(assets):
    return hashlib.sha256(json.dumps(sorted([(a.id, a.sha256, a.size) for a in assets]),
        separators=(',', ':')).encode()).hexdigest()


def report_row(db,user,report):
    result=public_row(report)
    if report.summary.get('evaluation_set_id'):
        from .models import EvaluationSet
        item=fetch(db,EvaluationSet,report.summary['evaluation_set_id'])
        if user.id not in {item.creator_id,item.reviewer_id}:
            summary={key:value for key,value in report.summary.items() if key not in {'per_image','truth_asset_id','truth_sha256'}}
            summary['sampling']={key:value for key,value in summary.get('sampling',{}).items() if key!='sample_asset_ids'}
            result['summary']={**summary,'blind':True}
    return result


def blind_assignee(user):
    from .models import EvaluationSet
    return select(EvaluationSet.id).where(EvaluationSet.id==GovernanceReport.summary['evaluation_set_id'].as_string(),
        (EvaluationSet.creator_id==user.id)|(EvaluationSet.reviewer_id==user.id)).exists()


def options(data):
    result=data.model_dump(include={'truth_asset_id', 'batch_id', 'dedup_report_id', 'groups', 'seed', 'near_distance','evaluation_target'})
    if data.evaluation_set_id:result['evaluation_set_id']=data.evaluation_set_id
    return result


def source_groups(db, version_id, images, declared):
    if declared:return declared
    from .models import ImportItem
    rows=db.execute(select(ImportItem.asset_id,ImportItem.source_group).join(Asset,ImportItem.asset_id==Asset.id)
        .where(Asset.version_id==version_id)).all()
    mapping={identifier:group for identifier,group in rows if group}
    require({a.id for a in images}<=set(mapping),'导入清单缺少来源组，请为每张图显式补充；不能假定不同文件相互独立',422)
    return {a.id:mapping[a.id] for a in images}


def validate(db, user, version, assets, data, settings):
    ds = fetch(db, Dataset, version.dataset_id)
    blind=None
    if data.kind=='evaluate' and data.evaluation_set_id:
        from .blind_evaluation import require_evaluator
        blind=require_evaluator(db,user,data.evaluation_set_id,version.id)
    else:require(manage_dataset(db, user, ds), '治理任务仅由数据负责人创建')
    images = image_assets(assets)
    require(0 < len(images) <= settings.governance_max_images,
            f'当前版本有 {len(images)} 张图片，治理任务上限为 {settings.governance_max_images} 张；请联系管理员评估容量', 413)
    require(all(a.size <= 20 * 1024**2 for a in images), '治理图片每张最多 20MB', 413)
    if data.kind in {'split', 'curate'}:
        report = fetch(db, GovernanceReport, data.dedup_report_id)
        require(report.version_id == version.id and report.kind == 'dedup' and report.status == 'approved',
                '请先完成同版本去重报告的独立审核', 409)
        if data.kind == 'split':
            groups=source_groups(db,version.id,images,data.groups)
            require(set(groups) == {a.id for a in images} and all(0 < len(g) <= 100 for g in groups.values()),
                    '每张图片必须填写来源组；同一视频/连拍/已知主体使用相同组', 422)
    if data.kind == 'evaluate':
        from .review_templates import for_version
        require(for_version(version) is None,'此评测器仅适用于人物服饰；自定义目标不能套用帽子/袖长指标，请使用独立外部评测并保留非认证等级',409)
        truth = fetch(db, Asset, data.truth_asset_id)
        batch = fetch(db, ImageReviewBatch, data.batch_id)
        require((truth.version_id == version.id if not blind else truth.id==blind.truth_asset_id and truth.sha256==blind.truth_sha256)
                and truth.status == 'clean' and 0 < truth.size <= 2 * 1024**2,
                '真值文件须为当前版本内已检查的 JSON，最大 2MB', 422)
        require(batch.version_id == version.id and batch.status != 'discarded', '候选批次不属于当前版本', 422)
        if data.evaluation_target=='approved_annotations':
            from .quality_contract import annotation_snapshot
            annotation_snapshot(db, version.id, complete=True)


def annotation_manifest(db,batch_id):
    items=[(i.id,i.asset_id,i.revision,i.status,i.annotation) for i in db.scalars(select(ImageReviewItem).where(ImageReviewItem.batch_id==batch_id))]
    pages=[(p.id,p.asset_id,p.revision,p.status,p.snapshot) for p in db.scalars(select(ImageReviewImage).where(ImageReviewImage.batch_id==batch_id))]
    return hashlib.sha256(json.dumps({'items':sorted(items),'pages':sorted(pages)},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def dhash(image):
    resized=image.convert('L').resize((9,8))
    pixels=list(resized.get_flattened_data() if hasattr(resized,'get_flattened_data') else resized.getdata())
    return sum((pixels[y*9+x] > pixels[y*9+x+1]) << (y*8+x) for y in range(8) for x in range(8))


def duplicates(assets, storage, parameters, heartbeat):
    from .image_index import HammingIndex
    from .quality_contract import media_manifest
    index_tree, exact, candidates = HammingIndex(), {}, []
    images = sorted(image_assets(assets),key=lambda a:a.id)
    for index, asset in enumerate(images):
        heartbeat(force=index==0,progress=index*90//max(1,len(images)),
                  details={'phase':'dedup','processed_files':index,'total_files':len(images)})
        if asset.sha256 in exact:
            matches=[(exact[asset.sha256],0)]
        else:
            image=read_image(asset,storage)
            try:code=dhash(image)
            finally:image.close()
            matches=index_tree.search(code,parameters['near_distance'],
                max_comparisons=parameters.get('_max_comparisons',100000000),heartbeat=heartbeat)
        for other,distance in matches:
            pair_id = hashlib.sha256((other.id+asset.id).encode()).hexdigest()[:24]
            candidates.append({'id':pair_id,'left':other.id,'right':asset.id,
                'kind':'exact' if asset.sha256 == other.sha256 else 'near_candidate','distance':distance})
            require(len(candidates)<=parameters.get('_max_candidates',20000),
                    '重复候选超出配置容量；请降低近重复距离或分配更大审核预算，任务未标为完成',413)
        if asset.sha256 not in exact:
            exact[asset.sha256]=asset
            index_tree.add(code,asset)
    return {'method':'sha256-bktree-dhash64-v2','input_manifest':manifest(assets),'media_manifest':media_manifest(assets),
            'images':len(images),'pairs':candidates,'complete':True,'raw_files_deleted':False,
            'search_comparisons':index_tree.comparisons,
            'exact_strategy':'one representative per SHA256; connected exact groups',
            'limitations':['感知哈希候选需要人工核对；没有部署嵌入近重复检测']}


def split_images(db, assets, parameters):
    report = fetch(db, GovernanceReport, parameters['dedup_report_id'])
    from .quality_contract import media_manifest
    require(report.status == 'approved' and report.summary.get('media_manifest') == media_manifest(assets), '去重依据已变化', 409)
    images = image_assets(assets)
    version_id=fetch(db,GovernanceReport,parameters['dedup_report_id']).version_id
    group_mapping=source_groups(db,version_id,images,parameters['groups'])
    parent = {a.id:a.id for a in images}
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def merge(a,b):
        parent[root(b)] = root(a)
    groups = {}
    for asset in images:
        group = group_mapping[asset.id]
        if group in groups:
            merge(asset.id, groups[group])
        groups[group] = asset.id
    accepted = set(report.decision['duplicate_pairs'])
    for pair in report.summary['pairs']:
        if pair['id'] in accepted:
            merge(pair['left'], pair['right'])
        elif pair['kind'] == 'exact':
            merge(pair['left'], pair['right'])
    clusters = {}
    for asset in images:
        clusters.setdefault(root(asset.id),[]).append(asset.id)
    keys = sorted(clusters)
    random.Random(parameters['seed']).shuffle(keys)
    require(len(keys) >= 3, '至少需要三个独立来源组才能划分训练/验证/测试集', 409)
    count = len(keys)
    validation = max(1, round(count*.1))
    test = max(1, round(count*.1))
    entries = []
    for index, group in enumerate(keys):
        name = 'test' if index < test else 'validation' if index < test+validation else 'train'
        for identifier in sorted(clusters[group]):
            entries.append({'asset_id':identifier,'group':group,'split':name})
    return {'method':'group-split-v1','input_manifest':manifest(assets),'media_manifest':media_manifest(assets),'seed':parameters['seed'],
        'entries':entries,'groups':count,'ratio_basis':'groups','dedup_report_id':report.id,
        'manifest_sha256':hashlib.sha256(json.dumps(entries,sort_keys=True).encode()).hexdigest(),
        'limitations':['按已声明来源组与已确认重复关系隔离；不证明所有隐含主体或近重复均已识别']}


def retained_images(db, assets, parameters):
    report = fetch(db, GovernanceReport, parameters['dedup_report_id'])
    from .quality_contract import media_manifest
    require(report.status == 'approved' and report.summary.get('media_manifest') == media_manifest(assets), '去重依据已变化', 409)
    images = image_assets(assets)
    parent = {a.id:a.id for a in images}
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    accepted=set(report.decision['duplicate_pairs'])
    for pair in report.summary['pairs']:
        if pair['id'] in accepted or pair['kind'] == 'exact':
            left,right=root(pair['left']),root(pair['right'])
            parent[max(left,right)]=min(left,right)
    return [a for a in images if root(a.id)==a.id]


def quality_gate(summary, settings, policy=None):
    if policy:
        from types import SimpleNamespace
        settings = SimpleNamespace(**{'quality_' + key: value for key, value in policy.items() if key.startswith('min_')})
    failures=[]
    if policy and policy.get('require_blind_evaluation') and not summary.get('evaluation_set_id'):
        failures.append('组织策略要求独立盲测，常规报告不能替代')
    if summary.get('evaluation_target')!='approved_annotations':failures.append('发布质量验收须评测最终人工标注，模型诊断不能替代')
    if summary.get('unique_images',0)<settings.quality_min_truth_images:failures.append('独立且不重复的真值图片数量不足')
    for name in ('hat','short_sleeves'):
        metric=summary.get('attributes',{}).get(name,{})
        if metric.get('tp',0)+metric.get('fn',0)<settings.quality_min_positives:failures.append(name+':正类样本不足')
        if metric.get('truth_negative',0)<settings.quality_min_negatives:failures.append(name+':负类样本不足')
        if metric.get('coverage',0)<settings.quality_min_attribute_coverage:failures.append(name+':确定属性覆盖不足')
        for key,minimum in [('precision',settings.quality_min_precision),('recall',settings.quality_min_recall)]:
            if metric.get(key) is None or metric[key]<minimum:failures.append(name+':'+key+'未达标')
    if summary.get('detection',{}).get('recall') is None or summary['detection']['recall']<settings.quality_min_recall:
        failures.append('人物定位召回未达标')
    if summary.get('detection',{}).get('precision') is None or summary['detection']['precision']<settings.quality_min_precision:
        failures.append('人物定位精确率未达标')
    return {'passed':not failures,'failures':failures,'policy':{
        'min_images':settings.quality_min_truth_images,'min_positives':settings.quality_min_positives,
        'min_precision':settings.quality_min_precision,'min_recall':settings.quality_min_recall,
        'min_negatives':settings.quality_min_negatives,'min_attribute_coverage':settings.quality_min_attribute_coverage},
        'coverage':['hat','short_sleeves','person_iou50'], 'limitations':['仅覆盖报告所列属性与真值分布，不是所有用途的质量保证']}


def iou(a,b):
    x,y,w,h=a;u,v,s,t=b
    intersection=max(0,min(x+w,u+s)-max(x,u))*max(0,min(y+h,v+t)-max(y,v))
    return intersection/(w*h+s*t-intersection) if w*h+s*t > intersection else 0


def evaluate(db, assets, storage, parameters, heartbeat=lambda *args,**kwargs:None):
    evaluation_target=parameters.get('evaluation_target','model_candidate')
    signature=annotation_manifest(db,parameters['batch_id'])
    from .quality_contract import annotation_snapshot
    version_id = fetch(db, ImageReviewBatch, parameters['batch_id']).version_id
    snapshot = annotation_snapshot(db, version_id, complete=evaluation_target=='approved_annotations')
    approved_by_asset={}
    for item in snapshot['items']:approved_by_asset.setdefault(item.asset_id,[]).append(item)
    if evaluation_target=='approved_annotations':
        require(snapshot['hash'] == parameters.get('version_annotation_manifest'), '整版标注快照已变化，请重新评测', 409)
    if evaluation_target=='approved_annotations':require(signature==parameters.get('annotation_manifest'),'最终标注已变化，请创建新评测任务',409)
    truth_asset = fetch(db, Asset, parameters['truth_asset_id'])
    require(truth_asset.status=='clean' and 0<truth_asset.size<=2*1024**2,'真值文件不可用',409)
    raw = b''.join(storage.chunks(truth_asset.object_key,0,truth_asset.size-1))
    require(hashlib.sha256(raw).hexdigest() == truth_asset.sha256, '真值文件校验失败', 409)
    payload = json.loads(raw)
    require(payload.get('schema_version') == 1 and isinstance(payload.get('images'),list), '真值结构无效', 422)
    known = {a.id:a for a in image_assets(assets)}
    require(0 < len(payload['images']) <= len(known), '真值数量无效', 422)
    seen, total_truth, total_pred, matched = set(), 0, 0, 0
    seen_sha=set()
    attributes = {name:{'tp':0,'fp':0,'fn':0,'tn':0,'unclear':0,'matched':0,'truth_positive':0,'truth_negative':0,'answered':0} for name in ('hat','short_sleeves')}
    per_image=[]
    for index,truth in enumerate(payload['images']):
        heartbeat(True,progress=index*90//len(payload['images']))
        identifier = truth['asset_id']
        require(identifier in known and identifier not in seen, '真值图片不存在或重复', 422)
        seen.add(identifier)
        require(known[identifier].sha256 not in seen_sha,'真值含完全重复图片，请先去重，避免评测被重复样本放大',422)
        seen_sha.add(known[identifier].sha256)
        require(isinstance(truth.get('persons'),list) and len(truth['persons']) <= 200, '真值人数无效', 422)
        image = read_image(known[identifier],storage)
        targets = [Annotation.model_validate(p).model_dump() for p in truth['persons']]
        from .image_reviews import checked_annotation
        for target in targets:
            checked_annotation(target,*image.size)
            for metric,field,positive in [('hat','hat','hat'),('short_sleeves','sleeve','short_sleeves')]:
                value=target['labels'][field]
                if value!='unclear':attributes[metric]['truth_positive' if value==positive else 'truth_negative']+=1
        if evaluation_target=='approved_annotations':
            items = approved_by_asset.get(identifier,[])
        else:
            items=db.scalars(select(ImageReviewItem).where(ImageReviewItem.batch_id==parameters['batch_id'],ImageReviewItem.asset_id==identifier)).all()
        candidates=[i.annotation for i in items if i.status=='approved'] if evaluation_target=='approved_annotations' else [i.predicted for i in items if i.predicted]
        pairs=sorted([(iou(t['bbox'],p['bbox']),ti,pi) for ti,t in enumerate(targets)
            for pi,p in enumerate(candidates)],reverse=True)
        used_t, used_p = set(), set()
        for overlap,ti,pi in pairs:
            if overlap < .5 or ti in used_t or pi in used_p:
                continue
            used_t.add(ti);used_p.add(pi);matched+=1
            for metric,field,positive in [('hat','hat','hat'),('short_sleeves','sleeve','short_sleeves')]:
                actual, guess = targets[ti]['labels'][field], candidates[pi]['labels'][field]
                if actual == 'unclear':
                    continue
                values=attributes[metric];values['matched']+=1
                if guess == 'unclear':
                    values['unclear']+=1
                    if actual == positive:values['fn']+=1
                else:
                    values['answered']+=1
                    values['tp' if actual == positive and guess == positive else
                           'fn' if actual == positive else 'fp' if guess == positive else 'tn']+=1
        for ti,target in enumerate(targets):
            if ti not in used_t:
                for metric,field,positive in [('hat','hat','hat'),('short_sleeves','sleeve','short_sleeves')]:
                    if target['labels'][field] == positive:attributes[metric]['fn']+=1
        for pi,candidate in enumerate(candidates):
            if pi not in used_p:
                for metric,field,positive in [('hat','hat','hat'),('short_sleeves','sleeve','short_sleeves')]:
                    if candidate['labels'][field]==positive:attributes[metric]['fp']+=1
        total_truth+=len(targets);total_pred+=len(candidates)
        per_image.append({'asset_id':identifier,'truth_persons':len(targets),'predicted_persons':len(candidates),
                          'matched':len(used_t),'missed':len(targets)-len(used_t)})
    for values in attributes.values():
        tp,fp,fn=values['tp'],values['fp'],values['fn']
        values['precision']=tp/(tp+fp) if tp+fp else None
        values['recall']=tp/(tp+fn) if tp+fn else None
        values['f1']=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
        total=values['truth_positive']+values['truth_negative']
        values['coverage']=values['answered']/total if total else 0
    batch=fetch(db,ImageReviewBatch,parameters['batch_id'])
    return {'method':'truth-iou-greedy-v1','evaluation_target':evaluation_target,'annotation_manifest':signature,
        'input_manifest':manifest(assets),'truth_asset_id':truth_asset.id,
        'version_annotation_manifest':snapshot['hash'], 'annotation_scope':'version',
        'sampling':{'population_images':len(snapshot['images']), 'sample_images':len(seen),
                    'sample_asset_ids':sorted(seen), 'method':'system-random-frozen' if parameters.get('evaluation_set_id') else 'explicit-independent-truth'},
        'publication_policy':parameters.get('publication_policy'),
        'evaluation_set_id':parameters.get('evaluation_set_id'),
        'truth_sha256':truth_asset.sha256,'batch_id':batch.id,'model_provenance':batch.provenance,
        'images':len(seen),'unique_images':len({known[i].sha256 for i in seen}),'attributes':attributes,'per_image':per_image,
        'detection':{'truth':total_truth,'predicted':total_pred,'matched_iou50':matched,
                    'recall':matched/total_truth if total_truth else None,'precision':matched/total_pred if total_pred else None},
        'limitations':['IoU>=0.5贪心匹配诊断，不是COCO mAP','真值真实性需要独立人工核对；颜色没有单独评测']}


@router.get('/evaluations')
def evaluation_inbox(db=Depends(get_db), user=Depends(interactive_user),
                     limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
    """按成员身份查询独立测评待办；不从全平台报告中先取一页再做权限过滤。"""
    reviewer_scope = and_(or_(Membership.role.in_({'admin', 'reviewer'}),and_(Membership.role=='evaluator',blind_assignee(user))),
                          Version.status.in_({'draft', 'rejected', 'pending_review'}))
    rows = db.execute(select(GovernanceReport, Version, Dataset, Job, Membership)
        .join(Version, GovernanceReport.version_id == Version.id)
        .join(Dataset, Version.dataset_id == Dataset.id)
        .join(Job, GovernanceReport.job_id == Job.id)
        .join(Membership, and_(Membership.space_id == Dataset.space_id,
                               Membership.user_id == user.id, Membership.active.is_(True)))
        .where(GovernanceReport.kind == 'evaluate', Dataset.status == 'active',
               or_(Dataset.owner_id == user.id, reviewer_scope))
        .order_by(case((GovernanceReport.status == 'pending', 0), else_=1),
                  GovernanceReport.created_at.desc(), GovernanceReport.id.desc())
        .offset(offset).limit(limit + 1)).all()
    items = []
    for report, version, dataset, job, membership in rows[:limit]:
        items.append({'id': report.id, 'version_id': version.id, 'version_number': version.number,
                      'dataset_id': dataset.id, 'dataset_name': dataset.name, 'space_id': dataset.space_id,
                      'status': report.status, 'version_status': version.status,
                      'evaluation_target': report.summary.get('evaluation_target'),
                      'quality_gate': report.summary.get('quality_gate'),
                      'created_at': report.created_at,
                      'can_review': (membership.role in {'admin', 'reviewer'} and not report.summary.get('evaluation_set_id') or membership.role=='evaluator' and bool(report.summary.get('evaluation_set_id')))
                          and dataset.owner_id != user.id and job.user_id != user.id
                          and version.status in {'draft', 'rejected'} and report.status == 'pending'})
    return {'items': items, 'has_more': len(rows) > limit}


@router.get('/versions/{version_id}')
def reports(version_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    version=fetch(db,Version,version_id)
    ds=fetch(db,Dataset,version.dataset_id);role=member(db,user,ds.space_id)
    query=select(GovernanceReport).where(GovernanceReport.version_id==version.id)
    if role and role.role=='evaluator':
        require(ds.status=='active','数据集不可用',403);query=query.where(blind_assignee(user))
    else:report_access(db,user,version,request.app.state.settings)
    identifiers=list(db.scalars(query.with_only_columns(GovernanceReport.id)
        .order_by(GovernanceReport.created_at.desc(),GovernanceReport.id.desc()).limit(100)))
    rows={r.id:r for r in db.scalars(select(GovernanceReport).where(GovernanceReport.id.in_(identifiers)))} if identifiers else {}
    return [report_row(db,user,rows[identifier]) for identifier in identifiers]


class ReviewReport(Input):
    duplicate_pairs: list[str] = Field(default_factory=list,max_length=2000)
    reviewed_pairs: list[str] = Field(default_factory=list,max_length=2000)
    approve: bool = True
    comment: str = Field(default='',max_length=2000)
    truth_verified: bool = False


@router.post('/reports/{report_id}/decision')
def decide(report_id: str, data: ReviewReport, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    from .workflows import locked_version
    report=fetch(db,GovernanceReport,report_id)
    version,ds=locked_version(db,report.version_id)
    report=fetch(db,GovernanceReport,report_id,lock=True)
    role=member(db,user,ds.space_id)
    pipeline=version.card.get('_review_pipeline')
    if pipeline:require(user.id in pipeline['reviewers'],'本版标注岗位不能审批报告',403)
    if pipeline and pipeline.get('report_id')==report.id:
        require(False,'此报告已进入联合去重标注，请在图片终审中确认；全部候选通过后自动汇总',409)
    require(role and (role.role in {'admin','reviewer'} or role.role=='evaluator' and report.summary.get('evaluation_set_id')) and ds.owner_id != user.id and
            fetch(db,Job,report.job_id).user_id != user.id,'需要另一位组织审核人')
    require(ds.status=='active' and version.status in {'draft','rejected'} and report.status=='pending',
            '报告或版本已冻结',409)
    if report.summary.get('evaluation_set_id'):
        from .blind_evaluation import access
        from .models import EvaluationSet
        item=fetch(db,EvaluationSet,report.summary['evaluation_set_id']);access(db,user,item)
        require(user.id==item.reviewer_id,'此盲测仅指定批准人能处理',403)
    if report.kind=='dedup':
        ids={p['id'] for p in report.summary['pairs']}
        require(set(data.duplicate_pairs)<=ids and len(set(data.duplicate_pairs))==len(data.duplicate_pairs),'重复对标识无效',422)
        if data.approve:
            require(set(data.reviewed_pairs)==ids and len(data.reviewed_pairs)==len(ids),'所有候选对必须明确判定，不能遗漏后直接通过',422)
            require({p['id'] for p in report.summary['pairs'] if p['kind']=='exact'}<=set(data.duplicate_pairs),
                'SHA256完全相同的文件必须归入重复组',422)
    else:
        require(not data.duplicate_pairs,'此报告不接受重复对决定',422)
    assets=db.scalars(select(Asset).where(Asset.version_id==version.id)).all()
    if report.kind in {'dedup','split'}:
        from .quality_contract import media_manifest
        require(report.summary.get('media_manifest')==media_manifest(assets),'版本图片已变化，请重新执行报告',409)
    else:
        require(report.summary['input_manifest']==manifest(assets),'版本文件已变化，请重新执行报告',409)
    if report.kind=='evaluate' and data.approve:
        require(data.truth_verified,'请独立核对真值来源和标注，不能仅凭模型输出批准评测',422)
        from .quality_contract import annotation_snapshot, publication_policy
        if report.summary.get('evaluation_set_id'):
            from .blind_evaluation import require_evaluator,access
            from .models import EvaluationSet,User
            item=fetch(db,EvaluationSet,report.summary['evaluation_set_id'])
            access(db,user,item)
            require(user.id==item.reviewer_id,'此盲测只允许指定的独立审核人批准',403)
            require_evaluator(db,fetch(db,User,item.creator_id),item.id,version.id)
        policy = publication_policy(db, version, request.app.state.settings)
        if report.summary.get('publication_policy'):
            require(report.summary['publication_policy']==policy,'组织质量策略已变化，请重新评测',409)
        gate=quality_gate(report.summary,request.app.state.settings,policy)
        if report.summary.get('evaluation_target')=='approved_annotations':
            require(report.summary['annotation_manifest']==annotation_manifest(db,report.summary['batch_id']),'最终标注已变化，不能批准旧报告',409)
            require(report.summary.get('version_annotation_manifest')==annotation_snapshot(db,version.id,complete=True)['hash'],
                    '整版标注已变化，不能批准旧报告',409)
        report.summary={**report.summary,'quality_gate':gate}
    report.status='approved' if data.approve else 'rejected'
    report.reviewer_id=user.id
    report.decision=data.model_dump()
    audit(db,user,'governance.decision',report.id,ds.space_id,kind=report.kind,status=report.status)
    db.commit()
    return public_row(report)


def report_access(db,user,version,settings):
    ds=fetch(db,Dataset,version.dataset_id)
    role=member(db,user,ds.space_id)
    staff=bool(role and role.role in {'admin','reviewer'})
    require(ds.status=='active' and (manage_dataset(db,user,ds) or
        staff and version.status in {'draft','rejected','pending_review'} or can_read_content(db,user,version,settings)))
    return ds,staff


@router.get('/versions/{version_id}/context')
def context(version_id: str, request: Request, db=Depends(get_db),user=Depends(interactive_user)):
    version=fetch(db,Version,version_id)
    ds=fetch(db,Dataset,version.dataset_id);role=member(db,user,ds.space_id)
    evaluator=bool(role and role.role=='evaluator' and ds.status=='active')
    if evaluator:staff=True
    else:ds,staff=report_access(db,user,version,request.app.state.settings)
    assets=db.scalars(select(Asset).where(Asset.version_id==version.id)).all()
    image_ids={a.id for a in image_assets(assets)}
    return {'version_id':version.id,'dataset_id':ds.id,'dataset_name':ds.name,'number':version.number,
        'space_id':ds.space_id,'owner_id':ds.owner_id,
        'joint_report_id':(version.card.get('_review_pipeline') or {}).get('report_id'),
        'image_count':len(image_ids),'governance_max_images':request.app.state.settings.governance_max_images,
        'dedup_max_candidates':request.app.state.settings.dedup_max_candidates,
        'my_role':role.role if role else None,
        'can_manage':manage_dataset(db,user,ds),'can_review':staff and ds.owner_id!=user.id,
        'mutable':version.status in {'draft','rejected'},'quality_gate_required':version.quality_tier=='certified' or bool(version.card.get('quality_gate_required')),
        'quality_tier':version.quality_tier,'source_approval':version.card.get('_source_approval'),
        'source':version.source,'license':version.license,
        'assets':[] if evaluator else [{'id':a.id,'filename':a.filename,'status':a.status,'image':a.id in image_ids} for a in assets],
        'batches':[{'id':b.id,'status':b.status} for b in db.scalars(select(ImageReviewBatch).where(
            ImageReviewBatch.version_id==version.id,ImageReviewBatch.status!='discarded'))]}


@router.get('/reports/{report_id}/assets/{asset_id}/preview', dependencies=[Depends(media_slot)])
def preview(report_id: str,asset_id: str,request: Request,db=Depends(get_db),user=Depends(interactive_user)):
    from fastapi.responses import Response
    import io
    report=fetch(db,GovernanceReport,report_id)
    version=fetch(db,Version,report.version_id)
    if report.summary.get('evaluation_set_id'):
        from .blind_evaluation import access
        from .models import EvaluationSet
        item=fetch(db,EvaluationSet,report.summary['evaluation_set_id']);access(db,user,item,True)
        require(user.id==fetch(db,Dataset,version.dataset_id).owner_id or asset_id in item.sample_ids,'不属于盲测样本',403)
    else:report_access(db,user,version,request.app.state.settings)
    asset=fetch(db,Asset,asset_id)
    job=fetch(db,Job,report.job_id)
    allowed={a['id'] for a in job.parameters['input_assets']}
    require(asset.version_id==version.id and asset.id in allowed,'图片不属于本报告',404)
    require(bool(image_assets([asset])),'仅支持受控图片预览',422)
    from .preview_cache import thumbnail
    out=thumbnail(request,asset,(1280,1280),85)
    audit(db,user,'governance.preview',asset.id,fetch(db,Dataset,version.dataset_id).space_id,report_id=report.id)
    db.commit()
    return Response(out,media_type='image/jpeg',headers={'Cache-Control':'no-store'})


@router.get('/reports/{report_id}/truth')
def truth_preview(report_id: str,request: Request,db=Depends(get_db),user=Depends(interactive_user)):
    report=fetch(db,GovernanceReport,report_id)
    version=fetch(db,Version,report.version_id)
    if not report.summary.get('evaluation_set_id'):report_access(db,user,version,request.app.state.settings)
    require(report.kind=='evaluate','此报告没有真值',404)
    blind=report.summary.get('evaluation_set_id')
    if blind:
        from .blind_evaluation import access
        from .models import EvaluationSet
        access(db,user,fetch(db,EvaluationSet,blind))
    asset=fetch(db,Asset,report.summary['truth_asset_id'])
    require((bool(blind) or asset.version_id==version.id) and asset.status=='clean' and asset.size<=2*1024**2,'真值不可用',409)
    raw=b''.join(request.app.state.storage.chunks(asset.object_key,0,asset.size-1))
    require(hashlib.sha256(raw).hexdigest()==report.summary['truth_sha256'],'真值已变化',409)
    from fastapi.responses import Response
    return Response(raw,media_type='application/json',headers={'Cache-Control':'no-store'})


class QualityPolicy(Input):
    required: bool


class SourceDecision(Input):
    approved: bool
    evidence_reference: str = Field(min_length=5,max_length=2000)
    reason: str = Field(min_length=5,max_length=2000)


@router.post('/versions/{version_id}/source-decision')
def source_decision(version_id:str,data:SourceDecision,db=Depends(get_db),user=Depends(interactive_user)):
    from .workflows import locked_version
    from .quality_contract import digest
    from .db import now
    version,ds=locked_version(db,version_id)
    role=member(db,user,ds.space_id)
    require(role and role.role in {'admin','reviewer'} and user.id!=ds.owner_id,'需要独立组织审核人')
    require(ds.status=='active' and version.status in {'draft','rejected'},'版本已冻结',409)
    approval={**data.model_dump(),'reviewer_id':user.id,'reviewed_at':now().isoformat()+'Z',
              'source_hash':digest({'source':version.source,'license':version.license})}
    version.card={**version.card,'_source_approval':approval}
    audit(db,user,'version.source_decision',version.id,ds.space_id,approved=data.approved)
    db.commit()
    return approval


@router.put('/versions/{version_id}/quality-policy')
def policy(version_id: str,data: QualityPolicy,db=Depends(get_db),user=Depends(interactive_user)):
    from .workflows import locked_version
    version,ds=locked_version(db,version_id)
    require(manage_dataset(db,user,ds) and ds.status=='active')
    require(version.status in {'draft','rejected'},'版本已冻结',409)
    from .models import Space
    require(data.required or version.quality_tier!='certified' and
            (fetch(db,Space,ds.space_id).quality_policy or {}).get('minimum_tier')!='certified',
            '认证等级或组织策略要求质量验收，不能关闭',409)
    version.card={**version.card,'quality_gate_required':data.required}
    audit(db,user,'quality_policy.change',version.id,ds.space_id,required=data.required)
    db.commit()
    return {'required':data.required}
