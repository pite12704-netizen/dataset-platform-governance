"""【学习标注：数据目录与版本】搜索目录、创建数据集、管理不可变版本、收藏和举报。

主链路：create_dataset → create_version → version_detail。
catalog_query 把发现权限下推到数据库，分页前先过滤；目录卡片批量加载版本摘要。
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import String, and_, case, cast, exists, false, func, or_, select

from . import schemas as s
from .common import audit, fetch, public_row, require
from .db import get_db
from .models import (Asset, Dataset, Favorite, Membership, Project, ProjectMember, Publication, QualityReport,
                     Report, Space, User, Version)
from .policy import can_read_content, can_view_version, discover, manage_dataset, member
from .security import enforce_scope, interactive_user, optional_user

router = APIRouter(tags=["数据集与版本"])


def version_row(version, private=False):
    result=public_row(version)
    from .quality_scope import person_clothing_scope
    result['quality_assessment_scope']=person_clothing_scope()
    from .review_templates import for_version,digest
    template=for_version(version)
    if template:
        result['review_template']=template
        result['quality_assessment_scope']={'template_digest':digest(template),
            'statement':'自定义目标模板：识别结果与人工审核记录不等于已通过独立准确率认证。'}
    if not private:result['card']={key:value for key,value in version.card.items() if not key.startswith('_')}
    return result


def dataset_row(db, ds):
    result = public_row(ds)
    versions = db.scalars(select(Version).where(Version.dataset_id == ds.id,
                         Version.status == "published").order_by(Version.number.desc())).all()
    result["published_versions"] = len(versions)
    result["latest_version_id"] = versions[0].id if versions else None
    result['latest_quality_tier']=versions[0].quality_tier if versions else None
    result["space_name"] = db.get(Space, ds.space_id).name
    return result


def latest_quality_report(db, version_id):
    """先排序小 ID，再投影摘要；逐文件正文不进入排序缓冲或版本详情。"""
    identifier=db.scalar(select(QualityReport.id).where(QualityReport.version_id==version_id)
        .order_by(QualityReport.created_at.desc(),QualityReport.id.desc()).limit(1))
    if identifier is None:return None
    fields=('method','model','total','checked','passed','failed','duplicate_files','not_checked')
    row=db.execute(select(QualityReport.id,QualityReport.job_id,QualityReport.created_at,
        *[QualityReport.summary[name].label(name) for name in fields]).where(QualityReport.id==identifier)).mappings().one()
    return {'id':row['id'],'job_id':row['job_id'],'created_at':row['created_at'],
            'summary':{name:row[name] for name in fields}}


def json_member(db, column, value):
    """精确匹配 JSON 数组元素；禁止 LIKE 子串匹配导致指定成员/项目越权。"""
    if db.bind.dialect.name == 'mysql':
        return func.json_contains(column, func.json_quote(value)) == 1
    elements = func.json_each(column).table_valued('value')
    return exists(select(1).select_from(elements).where(elements.c.value == value))


def catalog_query(db, user, settings, q="", modality=None, mine=False, favorites=False):
    """与 policy.discover 对齐；计数与分页必须使用同一个权限谓词。"""
    published = exists(select(1).where(Version.dataset_id == Dataset.id, Version.status == 'published'))
    visibility = [Dataset.visibility == 'public'] if settings.deployment == 'public' else []
    own = false()
    if user:
        membership = exists(select(1).where(Membership.space_id == Dataset.space_id,
            Membership.user_id == user.id, Membership.active.is_(True)))
        own = and_(Dataset.owner_id == user.id, membership)
        projects = exists(select(1).select_from(Project).join(ProjectMember, ProjectMember.project_id == Project.id)
            .join(Membership, Membership.space_id == Project.space_id)
            .where(ProjectMember.user_id == user.id, ProjectMember.active.is_(True),
                Membership.user_id == user.id, Membership.active.is_(True),
                json_member(db, Dataset.audience_projects, Project.id)))
        visibility.extend([and_(Dataset.visibility == 'organization', membership),
            and_(Dataset.visibility == 'selected', or_(json_member(db, Dataset.audience_users, user.id), projects))])
    query = select(Dataset).where(or_(own, and_(Dataset.status == 'active', published,
        or_(*visibility) if visibility else false()))).order_by(Dataset.created_at.desc(), Dataset.id.desc())
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where((Dataset.name.ilike(f"%{escaped}%", escape="\\")) |
                            (Dataset.description.ilike(f"%{escaped}%", escape="\\")))
    if modality:
        query = query.where(Dataset.modality == modality)
    if mine:
        query = query.where(Dataset.owner_id == (user.id if user else ""))
    if favorites:
        query = query.where(Dataset.id.in_(select(Favorite.dataset_id).where(
            Favorite.user_id == (user.id if user else ""))))
    return query


def catalog_rows(db, datasets):
    """固定三次查询生成当前页卡片，不逐条查询版本/空间。"""
    if not datasets:
        return []
    identifiers = [ds.id for ds in datasets]
    versions = db.execute(select(Version.dataset_id, func.count(Version.id), func.max(Version.number))
        .where(Version.dataset_id.in_(identifiers), Version.status == 'published').group_by(Version.dataset_id)).all()
    stats = {identifier: (count, number) for identifier, count, number in versions}
    latest = {dataset_id:(identifier,tier) for dataset_id,identifier,tier in db.execute(select(Version.dataset_id, Version.id,Version.quality_tier).where(
        Version.status == 'published', or_(*[and_(Version.dataset_id == identifier, Version.number == number)
            for identifier, (_, number) in stats.items()]))).all()} if stats else {}
    spaces = dict(db.execute(select(Space.id, Space.name).where(Space.id.in_({ds.space_id for ds in datasets}))).all())
    return [{**public_row(ds), 'published_versions': stats.get(ds.id, (0, 0))[0],
        'latest_version_id': latest.get(ds.id,(None,None))[0],
        'latest_quality_tier':latest.get(ds.id,(None,None))[1], 'space_name': spaces[ds.space_id]} for ds in datasets]


def search_catalog(db, user, settings, q="", modality=None, mine=False, favorites=False,
                   *, terms=(), access_mode=None, limit=100, offset=0):
    # Agent 检索也在数据库筛选/排序并限制候选数，不把整个组织目录搬入内存。
    query = catalog_query(db, user, settings, q, modality, mine, favorites).where(Dataset.status != 'blocked')
    if access_mode:
        query = query.where(Dataset.access_mode == access_mode)
    scores = []
    for term in sorted(set(terms), key=lambda t: (-len(t), t))[:32]:
        escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        match = or_(*[column.ilike(f'%{escaped}%', escape='\\') for column in
            (Dataset.name, Dataset.description, Dataset.category, cast(Dataset.tags, String))])
        scores.append(case((match, len(term)), else_=0))
    if scores:
        score = sum(scores)
        if not modality:
            query = query.where(score > 0)
        query = query.order_by(None).order_by(score.desc(), Dataset.created_at.desc(), Dataset.id.desc())
    return catalog_rows(db, list(db.scalars(query.offset(min(max(offset, 0), 5000)).limit(min(max(limit, 1), 100)))))


@router.get("/datasets")
async def list_datasets(request: Request, q: str = Query("", max_length=200), modality: str | None = None,
                  mine: bool = False, favorites: bool = False, offset: int = Query(0, ge=0),
                  limit: int = Query(24, ge=1, le=100)):
    from .db import run_sql
    return await run_sql(request, list_catalog, request, q, modality, mine, favorites, offset, limit)


def list_catalog(db, request, q, modality, mine, favorites, offset, limit):
    user = optional_user(request, db)
    enforce_scope(request, "catalog:read")
    query = catalog_query(db, user, request.app.state.settings, q, modality, mine, favorites)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    rows = list(db.scalars(query.offset(offset).limit(limit)))
    return {'items': catalog_rows(db, rows), 'total': total}


def validate_audience(db, data, settings):
    require(settings.deployment == "public" or data.visibility != "public", "私有部署禁止互联网公开", 400)
    if data.visibility == "selected":
        require(data.audience_users or data.audience_projects, "指定可见范围不能为空", 400)
    for user_id in data.audience_users:
        fetch(db, User, user_id)
    for project_id in data.audience_projects:
        p = fetch(db, Project, project_id)
        require(p.space_id == data.space_id, "可见项目必须属于数据集空间", 400)


@router.post("/datasets", status_code=201)
def create_dataset(data: s.DatasetInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    m = member(db, user, data.space_id)
    require(m and m.role in {"admin", "contributor"}, "需要空间贡献者权限")
    validate_audience(db, data, request.app.state.settings)
    ds = Dataset(**data.model_dump(), owner_id=user.id)
    db.add(ds)
    db.flush()
    audit(db, user, "dataset.create", ds.id, ds.space_id)
    db.commit()
    return public_row(ds)


@router.get("/datasets/{dataset_id}")
def dataset_detail(dataset_id: str, request: Request, db=Depends(get_db), user=Depends(optional_user)):
    enforce_scope(request, "catalog:read")
    ds = fetch(db, Dataset, dataset_id)
    require(discover(db, user, ds, request.app.state.settings), "数据集不存在或不可见", 404)
    versions = db.scalars(select(Version).where(Version.dataset_id == ds.id).order_by(Version.number.desc()))
    return {**dataset_row(db, ds), "can_manage": manage_dataset(db, user, ds),
            "versions": [version_row(v,manage_dataset(db,user,ds)) for v in versions if can_view_version(db, user, v, request.app.state.settings)]}


@router.put("/datasets/{dataset_id}")
def edit_dataset(dataset_id: str, data: s.DatasetInput, request: Request,
                  db=Depends(get_db), user=Depends(interactive_user)):
    ds = fetch(db, Dataset, dataset_id, lock=True)
    require(manage_dataset(db, user, ds))
    require(data.space_id == ds.space_id, "不能迁移数据集归属", 400)
    require(not db.scalar(select(Version.id).where(Version.dataset_id == ds.id,
                Version.status.in_(["published", "pending_review"]))), "发布或审核后元数据不可直接修改", 409)
    validate_audience(db, data, request.app.state.settings)
    validate_modality_change(db,ds,data.modality)
    for key, value in data.model_dump().items():
        setattr(ds, key, value)
    audit(db, user, "dataset.edit", ds.id, ds.space_id)
    db.commit()
    return public_row(ds)


def validate_modality_change(db,ds,modality):
    if modality==ds.modality:return
    from .checks import require_modality
    from .models import Upload
    for filename in db.scalars(select(Asset.filename).join(Version,Asset.version_id==Version.id)
            .where(Version.dataset_id==ds.id).execution_options(yield_per=1000)):
        require_modality(modality,filename)
    for filename in db.scalars(select(Upload.filename).join(Version,Upload.version_id==Version.id)
            .where(Version.dataset_id==ds.id,Upload.status.in_(['uploading','finalizing','aborting']))
            .execution_options(yield_per=1000)):
        require_modality(modality,filename)


@router.put('/datasets/{dataset_id}/modality')
def edit_modality(dataset_id:str,data:s.ModalityInput,db=Depends(get_db),user=Depends(interactive_user)):
    ds=fetch(db,Dataset,dataset_id,lock=True)
    require(manage_dataset(db,user,ds) and ds.status=='active')
    require(not db.scalar(select(Version.id).where(Version.dataset_id==ds.id,
        Version.status.in_(['published','pending_review']))),'发布或审核后的数据类型不能直接修改',409)
    validate_modality_change(db,ds,data.modality)
    previous=ds.modality;ds.modality=data.modality
    audit(db,user,'dataset.modality',ds.id,ds.space_id,before=previous,after=data.modality)
    db.commit();return {'modality':ds.modality}


@router.post("/datasets/{dataset_id}/versions", status_code=201)
def create_version(dataset_id: str, data: s.VersionInput, db=Depends(get_db), user=Depends(interactive_user)):
    ds = fetch(db, Dataset, dataset_id, lock=True)
    require(manage_dataset(db, user, ds) and ds.status == "active")
    number = (db.scalar(select(func.max(Version.number)).where(Version.dataset_id == ds.id)) or 0) + 1
    version = Version(dataset_id=ds.id, number=number, creator_id=user.id, **data.model_dump())
    db.add(version)
    db.flush()
    audit(db, user, "version.create", version.id, ds.space_id)
    db.commit()
    return public_row(version)


@router.get("/versions/{version_id}")
def version_detail(version_id: str, request: Request, db=Depends(get_db), user=Depends(optional_user)):
    enforce_scope(request, "catalog:read")
    version = fetch(db, Version, version_id)
    settings = request.app.state.settings
    require(can_view_version(db, user, version, settings), "版本不存在或不可见", 404)
    ds = db.get(Dataset, version.dataset_id)
    manager = manage_dataset(db, user, ds)
    content = can_read_content(db, user, version, settings)
    if getattr(request.state, "scopes", None) is not None and "content:read" not in request.state.scopes:
        manager, content = False, False
    count,size=db.execute(select(func.count(Asset.id),func.coalesce(func.sum(Asset.size),0)).where(Asset.version_id==version.id)).one()
    visible=select(Asset).where(Asset.version_id==version.id)
    if not manager:
        from .evidence_access import truth_condition
        visible=visible.where(~truth_condition())
    if not (manager or content):visible=visible.where(version.status=='published',Asset.preview_allowed.is_(True),Asset.status=='clean')
    visible_count=db.scalar(select(func.count()).select_from(visible.subquery()))
    assets = db.scalars(visible.order_by(Asset.id).limit(50)).all()
    report = latest_quality_report(db,version.id) if manager or content else None
    return {**version_row(version,manager), "dataset": dataset_row(db, ds), "can_manage": manager,
            "can_download": content, "asset_count": count, "total_bytes": int(size),
            'visible_asset_count':visible_count,'assets_has_more':visible_count>len(assets),
            "assets": [public_row(a, exclude=("checks",)) for a in assets if manager or content or
                       (version.status == "published" and a.preview_allowed and a.status == "clean")],
            "quality": {**report['summary'],'details_available':True,'job_id':report['job_id']} if report else None}


@router.get('/versions/{version_id}/assets')
def version_assets(version_id:str,request:Request,db=Depends(get_db),user=Depends(optional_user),
                   limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0,le=100000)):
    enforce_scope(request,'catalog:read')
    version=fetch(db,Version,version_id);settings=request.app.state.settings
    require(can_view_version(db,user,version,settings),'版本不可见',404)
    dataset=fetch(db,Dataset,version.dataset_id)
    content=can_read_content(db,user,version,settings)
    scopes=getattr(request.state,'scopes',None)
    content=content and (scopes is None or 'content:read' in scopes)
    query=select(Asset).where(Asset.version_id==version.id)
    if not manage_dataset(db,user,dataset):
        from .evidence_access import truth_condition
        query=query.where(~truth_condition())
    if not content:query=query.where(version.status=='published',Asset.preview_allowed.is_(True),Asset.status=='clean')
    total=db.scalar(select(func.count()).select_from(query.subquery()))
    rows=db.scalars(query.order_by(Asset.id).offset(offset).limit(limit+1)).all()
    return {'items':[public_row(a,exclude=('checks',)) for a in rows[:limit]],'total':total,'has_more':len(rows)>limit}


@router.put("/versions/{version_id}")
def edit_version(version_id: str, data: s.VersionInput, db=Depends(get_db), user=Depends(interactive_user)):
    dataset_id = db.scalar(select(Version.dataset_id).where(Version.id == version_id))
    require(dataset_id is not None, "记录不存在", 404)
    ds = fetch(db, Dataset, dataset_id, lock=True)
    v = fetch(db, Version, version_id, lock=True)
    require(manage_dataset(db, user, ds))
    require(v.status in {"draft", "rejected"}, "此版本已冻结", 409)
    from .quality_contract import TIERS
    require(TIERS[data.quality_tier]>=TIERS[v.quality_tier],'已选择的质量等级不能由提交者降低；请创建新的原始版本',409)
    values=data.model_dump()
    values['card']={**values['card'],**{k:value for k,value in v.card.items() if k.startswith('_')}}
    for key, value in values.items():
        setattr(v, key, value)
    db.commit()
    return public_row(v)


@router.post("/datasets/{dataset_id}/favorite")
def favorite(dataset_id: str, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    ds = fetch(db, Dataset, dataset_id)
    require(discover(db, user, ds, request.app.state.settings), "数据集不可见", 404)
    item = db.scalar(select(Favorite).where(Favorite.user_id == user.id, Favorite.dataset_id == ds.id))
    if item:
        db.delete(item)
    else:
        db.add(Favorite(user_id=user.id, dataset_id=ds.id))
    db.commit()
    return {"favorite": not bool(item)}


@router.post("/datasets/{dataset_id}/reports", status_code=201)
def report(dataset_id: str, data: s.ReportInput, request: Request,
           db=Depends(get_db), user=Depends(interactive_user)):
    ds = fetch(db, Dataset, dataset_id)
    require(discover(db, user, ds, request.app.state.settings), "数据集不可见", 404)
    item = Report(user_id=user.id, dataset_id=ds.id, reason=data.reason)
    db.add(item)
    audit(db, user, "dataset.report", ds.id, ds.space_id)
    db.commit()
    return {"message": "举报已提交"}
