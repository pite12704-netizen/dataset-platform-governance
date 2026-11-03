"""Typed, content-bound human-confirmed plans; model text never creates jobs directly."""
from types import SimpleNamespace
from sqlalchemy import select,func
from sqlalchemy.orm import load_only
from .common import fetch,require,audit
from .models import Asset,Dataset,Version,Job,Space,ImageReviewBatch,ImageReviewImage,Upload,Membership,User
from .policy import manage_dataset
from .quality_contract import digest,media_manifest
from .job_payloads import hydrate_job
from .workflows import locked_version

def input_files(db,version_id):
    return [SimpleNamespace(**r) for r in db.execute(select(Asset.id,Asset.filename,Asset.size,Asset.sha256,Asset.status)
        .where(Asset.version_id==version_id).order_by(Asset.id)).mappings()]

def vision_preview(db,user,version_id,settings,image_limit):
    from .vision import configured,fingerprint,image_assets,model_settings
    from .review_templates import for_version,digest as template_digest
    version,ds=locked_version(db,version_id)
    require(manage_dataset(db,user,ds) and ds.status=='active','仅当前数据负责人可安排识别')
    require(version.status in {'draft','rejected'},'版本已冻结',409)
    from .imports import require_complete
    require_complete(db,version_id)
    require(not db.scalar(select(Upload.id).where(Upload.version_id==version_id,Upload.status.in_(['uploading','finalizing','aborting'])).limit(1)),'请先完成或取消上传',409)
    configured(settings)
    assets=input_files(db,version_id)
    require(assets and all(a.status=='clean' for a in assets),'全部文件检查通过后才能安排识别',409)
    require(not db.scalar(select(Job.id).where(Job.version_id==version_id,Job.kind=='vision_plan',Job.status.in_(['running','queued'])).limit(1)),
        '已有视觉计划运行，请先查看或取消原计划',409)
    assigned=set()
    rows=db.execute(select(Job.id,Job.status,Job.attempts,ImageReviewBatch.status.label('batch_status'))
        .outerjoin(ImageReviewBatch,ImageReviewBatch.job_id==Job.id).where(Job.version_id==version_id,Job.kind=='vision')).all()
    for row in rows:
        if row.batch_status=='discarded' or row.status in {'failed','cancelled'} and row.attempts>=3:continue
        job=db.scalar(select(Job).where(Job.id==row.id).options(load_only(Job.parameters)))
        hydrate_job(db,job,fields=('parameters',))
        assigned.update(a['id'] for a in job.parameters.get('input_assets',[]))
    images=image_assets(assets);selected=[a for a in images if a.id not in assigned][:image_limit]
    require(selected,'没有尚未安排识别的图片；已失败任务请在任务中心重试',409)
    require(all(a.size<=20*1024**2 for a in selected),'识别图片须在20MiB以内',413)
    template=for_version(version)
    receipt={'schema':'confirmed-workflow-v1','kind':'vision','version_id':version.id,'status':version.status,
        'input_hash':digest([(a.id,a.size,a.sha256,a.status) for a in assets]),
        'selection_hash':digest([a.id for a in selected]),'pipeline_hash':fingerprint(settings,template),
        'template_hash':template_digest(template) if template else 'legacy-person-clothing-v1',
        'metadata_hash':digest([version.license,version.source]),'image_limit':image_limit,
        'shard_size':settings.vision_max_images,'active_shards':settings.vision_plan_active_shards,
        'space_active_shards':settings.space_max_active_vision_shards}
    summary={'plan_hash':digest(receipt),'receipt':receipt,'images':len(selected),'total_images':len(images),
        'input_bytes':sum(a.size for a in selected),'model':model_settings(settings).model_name,
        'template_name':template['name'] if template else '默认人物服饰',
        'attributes':[f['label'] for f in template['fields']] if template else ['帽子','袖长','上衣颜色'],
        'shard_size':settings.vision_max_images,'selection_method':'剩余图片按文件名、资产ID排序取前N张，非随机评测样本'}
    return version,ds,assets,selected,summary

def dedup_preview(db,user,version_id,settings,near_distance):
    from .tasks import validate_job
    from .schemas import JobInput
    version,ds=locked_version(db,version_id)
    version,ds,assets=validate_job(db,user,JobInput(version_id=version_id,kind='dedup',near_distance=near_distance,idempotency_key='agent-preview'),settings)
    receipt={'schema':'confirmed-workflow-v1','kind':'dedup','version_id':version.id,'status':version.status,
        'input_hash':digest(sorted((a.id,a.size,a.sha256,a.status) for a in assets)),
        'metadata_hash':digest([version.source,version.license]),'near_distance':near_distance,
        'max_candidates':settings.dedup_max_candidates,'max_comparisons':settings.dedup_max_comparisons}
    from .vision import image_assets
    return version,ds,assets,{'plan_hash':digest(receipt),'receipt':receipt,'images':len(image_assets(assets)),
        'near_distance':near_distance,'max_candidates':settings.dedup_max_candidates,'max_comparisons':settings.dedup_max_comparisons}

def enqueue_vision(db,user,version,ds,assets,selected,summary,action_id,settings):
    fetch(db,Space,ds.space_id,lock=True)
    active=db.scalar(select(func.count()).select_from(Job).where(Job.space_id==ds.space_id,Job.kind=='vision_plan',Job.status.in_(['queued','running'])))
    require(active<settings.space_max_vision_plans,'组织视觉计划已满，请稍后重试',429)
    plan=Job(space_id=ds.space_id,user_id=user.id,version_id=version.id,kind='vision_plan',status='running',
        idempotency_key='agent-'+action_id,parameters={'asset_ids':[a.id for a in selected],'cursor':0,'children':[],
        'shard_size':settings.vision_max_images,'pipeline_fingerprint':summary['receipt']['pipeline_hash'],
        'population_manifest':media_manifest(assets),'agent_action_id':action_id,'confirmed_plan_hash':summary['plan_hash']})
    db.add(plan);db.flush();audit(db,user,'agent.vision_plan.create',plan.id,ds.space_id,action_id=action_id,images=len(selected))
    return plan

def start_vision(app,plan_id):
    from .vision_plans import advance
    from .tasks import dispatch
    while True:
        children=advance(plan_id,app.state.db,app.state.settings)
        for identifier in children:dispatch(app,identifier)
        if app.state.settings.task_mode!='inline' or not children:break

def diagnose(db,user,settings,version_id):
    from .assistant_tools import visible_version
    version,ds=visible_version(db,user,settings,version_id)
    require(manage_dataset(db,user,ds),'流程诊断仅向当前数据负责人开放')
    files=dict(db.execute(select(Asset.status,func.count()).where(Asset.version_id==version_id).group_by(Asset.status)).all())
    total=sum(files.values());uploads=db.scalar(select(func.count()).select_from(Upload).where(Upload.version_id==version_id,Upload.status.in_(['uploading','finalizing','aborting'])))
    pages=dict(db.execute(select(ImageReviewImage.status,func.count()).join(ImageReviewBatch,ImageReviewImage.batch_id==ImageReviewBatch.id)
        .where(ImageReviewBatch.version_id==version_id,ImageReviewBatch.status!='discarded').group_by(ImageReviewImage.status)).all())
    jobs=[dict(r) for r in db.execute(select(Job.kind,Job.status,func.count().label('count')).where(Job.version_id==version_id)
        .group_by(Job.kind,Job.status)).mappings()]
    query=select(func.count()).select_from(User).where(User.active.is_(True),User.verified.is_(True),User.id!=ds.owner_id)
    if ds.visibility=='public':query=query.where(User.platform_admin.is_(True))
    else:query=query.join(Membership,Membership.user_id==User.id).where(Membership.space_id==ds.space_id,Membership.active.is_(True),Membership.role=='admin')
    administrators=db.scalar(query)
    from fastapi import HTTPException
    from .imports import require_complete
    try:require_complete(db,version_id);imports={'code':'imports','ok':True,'detail':'批量导入清单完整，或未使用批量导入'}
    except HTTPException as exc:imports={'code':'imports','ok':False,'detail':str(exc.detail)}
    checks=[{'code':'files','ok':total>0 and files.get('clean',0)==total,'detail':f'文件总数 {total}，检查通过 {files.get("clean",0)}，失败 {files.get("rejected",0)}'},
        {'code':'uploads','ok':uploads==0,'detail':f'未结束上传 {uploads}'},imports,
        {'code':'annotation','ok':not pages or set(pages)=={'approved'},'detail':f'整图待标注 {pages.get("pending",0)}，待独立确认 {pages.get("submitted",0)}，已确认 {pages.get("approved",0)}'},
        {'code':'publisher','ok':administrators>0,'detail':f'除负责人外的有效发布管理员 {administrators}，标注审核员不自动取得发布权'}]
    if version.quality_tier!='raw' or version.card.get('quality_gate_required'):
        checks.append({'code':'evidence','ok':None,'detail':'此版本启用质量或正式交付要求，需核对独立测评、标注快照与训练包的有效性；本摘要不冒充验收通过'})
    return {'version_id':version_id,'status':version.status,'checks':checks,'jobs':jobs,'facts_source':'实时数据库聚合',
        'limitations':['这是流程摘要，不替代发布时的整版覆盖率、质量、交付包和当前操作者权限校验',
        '没有标注待办不表示已完成识别；未运行的语义检查不等于通过'],
        'links':{'dataset':f'/datasets/{ds.id}?version={version_id}','reviews':f'/image-reviews/workbench/{version_id}','jobs':'/jobs','publications':'/requests'}}
