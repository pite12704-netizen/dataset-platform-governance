"""【学习标注：正式交付】从整版批准快照生成训练包，禁止手工文件冒充审核产物。"""
import hashlib
import json
import tempfile
import zipfile
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request, Query, HTTPException
from sqlalchemy import select, func

from .common import audit, fetch, require
from .db import get_db
from .models import Asset, Dataset, GovernanceReport, ImageReviewBatch, ImageReviewItem, ImageReviewImage, Job, Version
from .policy import can_read_content, manage_dataset
from .quality_contract import annotation_snapshot, digest, media_manifest
from .security import interactive_user, enforce_scope

router = APIRouter(prefix='/api/v1/deliveries', tags=['正式训练交付'])


def split_evidence(db, version, images):
    from .joint_review import config,images as source_images
    expected = media_manifest(source_images(db,version.id)) if config(version) and config(version).get('report_id') else media_manifest(images)
    for report in db.scalars(select(GovernanceReport).where(GovernanceReport.version_id == version.id,
            GovernanceReport.kind == 'split', GovernanceReport.status == 'approved')
            .order_by(GovernanceReport.created_at.desc(), GovernanceReport.id.desc())):
        if report.summary.get('media_manifest') == expected:
            entries = report.summary.get('entries', [])
            mapping = {e['asset_id']: 'val' if e['split']=='validation' else e['split'] for e in entries}
            selected={a.id for a in images}
            if selected <= set(mapping) and set(mapping.values()) <= {'train','val','test'}:
                mapping={key:value for key,value in mapping.items() if key in selected}
                return {'report_id': report.id, 'mapping': mapping, 'hash': digest(report.summary)}
    return None


def selected_image_ids(snapshot, selection):
    if not selection:return {a.id for a in snapshot['images']}
    if selection.get('has_person') is False:
        return {p.asset_id for p in snapshot['pages'] if p.snapshot.get('no_person')}
    fields={key:value for key,value in selection.items() if key!='has_person' and value is not None}
    return {i.asset_id for i in snapshot['items'] if i.status=='approved' and
            all(i.annotation['labels'].get(key)==value for key,value in fields.items())}


def release_contract(db, version, assets, selection=None):
    from .image_reviews import Labels
    from .governance import manifest
    from .quality_contract import blind_report_valid
    snapshot = annotation_snapshot(db, version.id, complete=True)
    split = split_evidence(db, version, snapshot['images'])
    evidence=None
    for report in db.scalars(select(GovernanceReport).where(GovernanceReport.version_id==version.id,
            GovernanceReport.kind=='evaluate',GovernanceReport.status=='approved')
            .order_by(GovernanceReport.created_at.desc(),GovernanceReport.id.desc())):
        summary=report.summary
        if (blind_report_valid(db,report) and summary.get('evaluation_target')=='approved_annotations' and
            summary.get('version_annotation_manifest')==snapshot['hash'] and
            summary.get('input_manifest')==manifest(assets) and summary.get('quality_gate',{}).get('passed')):
            evidence={'report_id':report.id,'quality_gate':summary['quality_gate'],
                'blind':bool(summary.get('evaluation_set_id')),
                'sampling':summary.get('sampling'),'publication_policy':summary.get('publication_policy'),
                'limitations':summary.get('limitations',[]),'truth_sha256':summary.get('truth_sha256')}
            break
    metadata = {'license': version.license, 'source': version.source, 'quality_tier': version.quality_tier,
                'schema': 'person-clothing-v1', 'label_schema':Labels.model_json_schema(),
                'normalization':'exif-orientation-rgb-png-no-metadata-v1',
                'coordinate_system': 'exif_transposed_pixels_xywh'}
    from .review_templates import for_version,digest as template_digest
    template=for_version(version)
    if template:metadata.update(schema='generic-object-review-v1',label_schema=template,template_digest=template_digest(template))
    return {'schema':'delivery-contract-v1', 'version_id':version.id, 'media_manifest':media_manifest(assets),
            'annotation_manifest':snapshot['hash'], 'metadata':metadata, 'split':split,
            'quality_evidence':evidence,'quality_limitations':['质量结论只覆盖所列任务、属性和真值分布；颜色无独立统计验收不能宣称已验证准确率'],
            'asset_manifest':digest(sorted((a.id,a.size,a.sha256) for a in assets)),
            'selection':selection or {},'selected_asset_ids':sorted(selected_image_ids(snapshot,selection or {}))}


def validate_release(db, user, version, assets, settings):
    ds = fetch(db, Dataset, version.dataset_id)
    require(manage_dataset(db,user,ds) and ds.status == 'active', '只有数据负责人可构建交付', 403)
    require(version.status in {'draft','rejected'}, '已冻结版本不能重新构建', 409)
    from .imports import require_complete
    require_complete(db,version.id)
    from .models import Upload
    require(not db.scalar(select(Upload.id).where(Upload.version_id==version.id,Upload.status.in_({'uploading','finalizing','aborting'})).limit(1)),
            '请先完成或取消上传',409)
    contract = release_contract(db,version,assets)
    from .review_templates import for_version
    require(not for_version(version) or version.quality_tier!='certified','自定义目标尚未接入独立认证评测，不能标记为 certified',409)
    snapshot=annotation_snapshot(db,version.id,complete=True)
    require(len(snapshot['images'])<=settings.delivery_max_images and len(snapshot['items'])<=settings.delivery_max_annotations,
            '版本超过当前交付构建容量，请拆分版本或完成扩容验证后调整限制',413)
    if version.quality_tier == 'certified':
        require(contract['split'], '认证交付需先完成按来源组隔离的数据划分及独立批准',409)


def release_reservation(db, version):
    snapshot = annotation_snapshot(db,version.id,complete=True)
    pages = {p.asset_id:p for p in snapshot['pages']}
    from .review_templates import for_version
    template=for_version(version)
    if template:
        # Arbitrary text fields and VLM prompts can outweigh a tiny image.
        annotations=sum(max(4096,len(json.dumps(i.annotation,ensure_ascii=False).encode())*3+4096) for i in snapshot['items'])
        schema_bytes=len(json.dumps(template,ensure_ascii=False).encode())*len(snapshot['images'])*2
    else:annotations=len(snapshot['items'])*4096;schema_bytes=0
    # Lossless EXIF-normalized PNG can be much larger than the original JPEG.
    return sum(max(a.size,pages[a.id].width*pages[a.id].height*4+65536) for a in snapshot['images']) + \
        annotations + schema_bytes + 4*1024**2


def build_release(db, job, assets, storage, run_token, heartbeat):
    from .image_reviews import read_image, checked_annotation
    version = fetch(db,Version,job.version_id)
    contract = release_contract(db,version,assets,job.parameters.get('selection'))
    require(contract==job.parameters['release_contract'],'标注或版本已变化，请重新构建',409)
    snapshot = annotation_snapshot(db,version.id,complete=True)
    images, annotations, checksums = [], [], []
    selected=set(contract['selected_asset_ids'])
    require(selected,'筛选结果为空，不能生成空交付包',409)
    by_asset = {}
    for item in snapshot['items']:
        if item.status=='approved':by_asset.setdefault(item.asset_id,[]).append(item)
    mapping = contract['split']['mapping'] if contract['split'] else {}
    with tempfile.TemporaryFile() as output:
        with zipfile.ZipFile(output,'w',zipfile.ZIP_STORED,allowZip64=True) as archive:
            for index,asset in enumerate(sorted((a for a in snapshot['images'] if a.id in selected),key=lambda a:a.id),1):
                heartbeat(True,progress=index*85//len(snapshot['images']))
                image = read_image(asset,storage)
                filename = f'images/{asset.id}.png'
                with tempfile.TemporaryFile() as normalized:
                    image.info.clear()
                    image.save(normalized,'PNG'); normalized.seek(0)
                    sha = hashlib.sha256()
                    info=zipfile.ZipInfo(filename,date_time=(1980,1,1,0,0,0));info.create_system=3;info.external_attr=0o600<<16
                    with archive.open(info,'w',force_zip64=True) as target:
                        while block := normalized.read(1024*1024):
                            heartbeat(); sha.update(block); target.write(block)
                    checksums.append({'path':filename,'sha256':sha.hexdigest()})
                images.append({'id':index,'file_name':filename,'width':image.width,'height':image.height,
                    'source_asset_id':asset.id,'source_sha256':asset.sha256,'split':mapping.get(asset.id,'unpartitioned')})
                for item in sorted(by_asset.get(asset.id,[]),key=lambda i:i.id):
                    from .review_templates import for_version
                    template=for_version(version)
                    value = checked_annotation(item.annotation,image.width,image.height,template)
                    box = value['bbox']
                    category_id=next((n+1 for n,c in enumerate(template['classes']) if c['key']==value['category']),None) if template else 1
                    annotations.append({'id':len(annotations)+1,'image_id':index,'category_id':category_id,'bbox':box,
                        'area':box[2]*box[3],'iscrowd':0,'attributes':value['labels'],
                        'source_item_id':item.id,'revision':item.revision})
                image.close()
                require(output.tell()<=job.reserved_bytes,'交付产物超过预留容量',409)
            from .review_templates import for_version
            template=for_version(version)
            categories=[{'id':n+1,'name':c['key'],'display_name':c['label']} for n,c in enumerate(template['classes'])] if template else [{'id':1,'name':'person'}]
            payload = {'images':images,'annotations':annotations,'categories':categories}
            if template:payload['review_template']=template
            if job.parameters['delivery_format']=='reviewed-json':
                payload = {'schema_version':1,'kind':'version-approved-annotations',**payload}
            raw = json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
            from .export_formats import write,extra_files
            write(archive,'annotations.json',raw,checksums)
            extra_files(archive,payload,job.parameters['delivery_format'],checksums)
            write(archive,'LICENSE.txt',version.license+'\n\nSource: '+version.source,checksums)
            positive_ids={a['image_id'] for a in annotations}
            write(archive,'delivery.json',json.dumps({'contract':contract,'checksums':checksums,
                'format':job.parameters['delivery_format'],'images':len(images),'persons':len(annotations),
                'negative_images':sum(i['id'] not in positive_ids for i in images),
                'transformations':['图像按 EXIF 校正并去除 EXIF/GPS，原图保留','YOLO 按审核坐标归一化，仅裁去边界容差≤0.01像素；原始框在 annotations.json 保留'],
                'limitations':['YOLO 标准标签只包含人物检测，服饰属性保留在 annotations.json；VLM 回答仅来自已审核标签',
                               '无有效划分时标为 unpartitioned，不伪造训练验证划分']},ensure_ascii=False,sort_keys=True),[])
        size = output.tell(); require(size<=job.reserved_bytes,'交付产物超过预留容量',409)
        output.seek(0); checksum=hashlib.sha256()
        while block:=output.read(1024*1024):checksum.update(block);heartbeat()
        output.seek(0); storage.put_file(f'packages/{job.id}/{run_token}',output,progress=heartbeat)
    return {'artifact_id':run_token,'size':size,'sha256':checksum.hexdigest(),'files':len(images),
            'format':job.parameters['delivery_format'],'contract':contract,'images':len(images),'persons':len(annotations)}


@router.get('/versions/{version_id}/attributes')
def attributes(version_id:str,request:Request,hat:str|None=None,sleeve:str|None=None,upper_color:str|None=None,
               has_person:bool|None=None,limit:int=Query(24,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),
               db=Depends(get_db),user=Depends(interactive_user)):
    from .schemas import AttributeFilter
    from pydantic import ValidationError
    try:selection=AttributeFilter(hat=hat,sleeve=sleeve,upper_color=upper_color,has_person=has_person).model_dump(exclude_none=True)
    except ValidationError:require(False,'属性筛选值无效',422)
    enforce_scope(request,'content:read')
    version=fetch(db,Version,version_id)
    require(can_read_content(db,user,version,request.app.state.settings))
    batches=select(ImageReviewBatch.id).where(ImageReviewBatch.version_id==version.id,ImageReviewBatch.status=='completed')
    query=select(Asset,ImageReviewImage).join(ImageReviewImage,ImageReviewImage.asset_id==Asset.id).where(
        Asset.version_id==version.id,Asset.status=='clean',ImageReviewImage.batch_id.in_(batches),ImageReviewImage.status=='approved')
    pipeline=version.card.get('_review_pipeline')
    if pipeline and pipeline.get('report_id'):
        report=fetch(db,GovernanceReport,pipeline['report_id'])
        if report.status=='approved':
            kept=annotation_snapshot(db,version.id,complete=False)['images']
            query=query.where(Asset.id.in_([a.id for a in kept]))
    if has_person is False:query=query.where(ImageReviewImage.snapshot['no_person'].as_boolean().is_(True))
    elif selection:
        match=select(ImageReviewItem.id).where(ImageReviewItem.asset_id==Asset.id,
            ImageReviewItem.batch_id==ImageReviewImage.batch_id,ImageReviewItem.status=='approved')
        for field in ('hat','sleeve','upper_color'):
            if selection.get(field):match=match.where(ImageReviewItem.annotation['labels'][field].as_string()==selection[field])
        query=query.where(match.exists())
    total=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.execute(query.order_by(Asset.id).offset(offset).limit(limit+1)).all()
    return {'items':[{'id':a.id,'filename':a.filename,'sha256':a.sha256,'width':p.width,'height':p.height,
                      'no_person':p.snapshot.get('no_person',False)} for a,p in rows[:limit]],
            'total':total,'has_more':len(rows)>limit,'selection':selection,
            'semantics':'同一个已审核人物同时满足所有属性；仅返回整图已独立批准的样本'}


def current_release(db, version, assets):
    from .job_payloads import hydrate_job
    if version.status in {'published','withdrawn'}:
        if '_release_receipt' in version.card:
            return published_release(db, version)
        pinned=version.card.get('_release_build_id')
        if pinned:
            job=db.get(Job,pinned)
            return job if job and frozen_release_valid(db,version,assets,job) and job.result.get('sha256')==version.card.get('_release_sha256') else None
        for job in db.scalars(select(Job).where(Job.version_id==version.id,Job.kind=='release',Job.status=='completed').order_by(Job.created_at.desc(),Job.id.desc())):
            hydrate_job(db,job)
            if not job.parameters.get('selection') and frozen_release_valid(db,version,assets,job):return job
        return None
    contract = release_contract(db,version,assets)
    for job in db.scalars(select(Job).where(Job.version_id==version.id,Job.kind=='release',Job.status=='completed')
                          .order_by(Job.created_at.desc(),Job.id.desc())):
        hydrate_job(db,job)
        if job.artifact_bytes and job.result.get('contract')==contract:return job
    return None


def frozen_release_valid(db,version,assets,job):
    from .job_payloads import hydrate_job
    if job is not None:hydrate_job(db,job)
    if version.status in {'published','withdrawn'} and '_release_receipt' in version.card:
        pinned=published_release(db,version)
        return bool(pinned and pinned.id==job.id)
    from .quality_contract import blind_report_valid
    if job.version_id!=version.id or job.kind!='release' or job.status!='completed' or not job.artifact_bytes:return False
    contract=job.result.get('contract',{});metadata=contract.get('metadata',{})
    if any(metadata.get(key)!=getattr(version,key) for key in ('license','source','quality_tier')):return False
    if contract.get('asset_manifest')!=digest(sorted((a.id,a.size,a.sha256) for a in assets)):return False
    if contract.get('annotation_manifest')!=annotation_snapshot(db,version.id,complete=True)['hash']:return False
    proof=contract.get('quality_evidence')
    if proof:
        report=db.get(GovernanceReport,proof['report_id'])
        if not report or report.status!='approved' or not blind_report_valid(db,report):return False
    return True


def release_receipt(version, job):
    """Called only after full validation while Dataset/Version/Job are locked."""
    contract=job.result['contract']
    return {'schema':'published-release-v1','build_id':job.id,'sha256':job.result['sha256'],
        'size':job.result['size'],'files':job.result.get('files'),'format':job.result.get('format'),
        'images':job.result.get('images'),'persons':job.result.get('persons'),
        'version_manifest':version.manifest_hash,'annotation_manifest':contract['annotation_manifest'],
        'metadata_hash':digest({key:getattr(version,key) for key in ('license','source','quality_tier')}),
        'report_id':(contract.get('quality_evidence') or {}).get('report_id')}


def published_release(db, version):
    """Bounded published lookup. Mutable authorization/evidence is never cached.

    Published content is API-immutable. Legacy versions without a receipt continue
    to use full validation; do not fabricate receipts for historical content.
    """
    receipt=version.card.get('_release_receipt',{})
    if receipt.get('schema')!='published-release-v1' or receipt.get('build_id')!=version.card.get('_release_build_id'):
        return None
    if receipt.get('version_manifest')!=version.manifest_hash or receipt.get('sha256')!=version.card.get('_release_sha256'):
        return None
    if receipt.get('metadata_hash')!=digest({key:getattr(version,key) for key in ('license','source','quality_tier')}):
        return None
    row=db.execute(select(Job.id,Job.version_id,Job.user_id,Job.space_id,Job.status,Job.kind,Job.artifact_bytes,
        Job.result['sha256'].as_string(),Job.result['size'].as_integer(),Job.result['artifact_id'].as_string())
        .where(Job.id==receipt['build_id'])).one_or_none()
    if not row:return None
    identifier,version_id,user_id,space_id,status,kind,amount,sha,size,token=row
    if (version_id!=version.id or status!='completed' or kind!='release' or not amount or
        amount!=size or size!=receipt.get('size') or sha!=receipt.get('sha256') or not token):return None
    if receipt.get('report_id'):
        proof=db.execute(select(GovernanceReport.status,GovernanceReport.summary['evaluation_set_id'].as_string(),
            GovernanceReport.summary['truth_sha256'].as_string(),GovernanceReport.summary['version_annotation_manifest'].as_string())
            .where(GovernanceReport.id==receipt['report_id'],GovernanceReport.version_id==version.id)).one_or_none()
        if not proof or proof[0]!='approved' or proof[3]!=receipt['annotation_manifest']:return None
        from .quality_contract import blind_report_valid
        if not blind_report_valid(db,SimpleNamespace(summary={'evaluation_set_id':proof[1],'truth_sha256':proof[2]})):return None
    return SimpleNamespace(id=identifier,user_id=user_id,space_id=space_id,version_id=version_id,status=status,kind=kind,
        artifact_bytes=amount,result={'sha256':sha,'size':size,'artifact_id':token,'files':receipt.get('files'),
            'format':receipt.get('format'),'images':receipt.get('images'),'persons':receipt.get('persons')})


@router.get('/versions/{version_id}')
def release_info(version_id:str, request:Request, db=Depends(get_db), user=Depends(interactive_user)):
    enforce_scope(request,'content:read')
    version=fetch(db,Version,version_id)
    require(can_read_content(db,user,version,request.app.state.settings))
    try:
        job=(published_release(db,version) if version.status in {'published','withdrawn'} and '_release_receipt' in version.card
             else current_release(db,version,db.scalars(select(Asset).where(Asset.version_id==version.id)).all()))
    except HTTPException as exc:
        if exc.status_code!=409:raise
        return {'available':False,'build':None,'reason':str(exc.detail)}
    return {'available':bool(job),'build':{'id':job.id,**job.result} if job else None}


def download_descriptor(db, version_id, request, record=True):
    from .security import current_user, optional_user
    from .downloads import byte_range
    user=current_user(optional_user(request,db));enforce_scope(request,'content:read')
    version=fetch(db,Version,version_id)
    require(can_read_content(db,user,version,request.app.state.settings),'没有此版本的内容授权')
    job=(published_release(db,version) if version.status in {'published','withdrawn'} and '_release_receipt' in version.card
         else current_release(db,version,db.scalars(select(Asset).where(Asset.version_id==version.id)).all()))
    require(job,'没有与当前批准快照一致的正式交付包',409)
    start,end,partial=byte_range(request.headers.get('range'),job.result['size'])
    if record:audit(db,user,'delivery.download',job.id,job.space_id);db.commit()
    headers={'Content-Disposition':f'attachment; filename="delivery-{version.id}.zip"',
        'Content-Length':str(end-start+1),'Accept-Ranges':'bytes','Cache-Control':'no-store',
        'ETag':'"'+job.result['sha256']+'"'}
    if partial:headers['Content-Range']=f"bytes {start}-{end}/{job.result['size']}"
    return {'key':f"packages/{job.id}/{job.result['artifact_id']}",'start':start,'end':end,'headers':headers,
            'status_code':206 if partial else 200,'media_type':'application/zip'}


@router.get('/versions/{version_id}/download')
async def download(version_id:str,request:Request):
    from .db import run_db
    from .async_storage import download_response
    descriptor=await run_db(request,download_descriptor,version_id,request)
    return await download_response(request,descriptor,lambda db:download_descriptor(db,version_id,request,False))
