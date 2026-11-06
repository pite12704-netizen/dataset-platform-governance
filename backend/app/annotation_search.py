"""按已独立复核的同一目标标签检索，不用文字相似度代替图像事实。"""
from sqlalchemy import select, func, or_, and_
from .models import Asset, Dataset, Version, ImageReviewItem as Item, ImageReviewImage as Page, ImageReviewBatch as Batch
from .policy import can_read_content

# 仅解释稳定字段和明确枚举；未提供字段、unclear、未复核不会被当成否定值。
FIELDS = {
 'male': (('gender','sex'), ('male','man','男性','男')),
 'female': (('gender','sex'), ('female','woman','女性','女')),
 'hat': (('hat',), ('hat','yes','true','戴帽子','有帽子')),
 'no_hat': (('hat',), ('no_hat','no','false','未戴帽子','不戴帽子','无帽','没有帽子')),
 'short_sleeve': (('sleeve',), ('short_sleeves','short','短袖')),
 'long_sleeve': (('sleeve',), ('long_sleeves','long','长袖')),
 'short_pants': (('pants',), ('short_pants','short','短裤')),
 'long_pants': (('pants',), ('long_pants','long','长裤')),
 'elderly': (('age_group',), ('elderly','older_adult','老人','老年人','老年')),
 'child': (('age_group',), ('child','儿童','小孩')),
}


def candidates(db,user,settings,version_ids,concepts,limit,predicate=None):
    if not concepts or not all(c in FIELDS for c in concepts):return [],[]
    clauses=[]
    for concept in concepts:
        fields,values=FIELDS[concept]
        clauses.append(or_(*[Item.annotation['labels'][field].as_string().in_(values) for field in fields]))
    query=select(Batch.version_id,func.count(func.distinct(Item.asset_id))).select_from(Item).join(Batch,Item.batch_id==Batch.id).join(
        Page,and_(Page.batch_id==Item.batch_id,Page.asset_id==Item.asset_id)).join(Asset,Asset.id==Item.asset_id).where(
        Batch.version_id.in_(version_ids),Batch.status!='discarded',Item.status=='approved',Page.status=='approved',
        Item.reviewer_id.is_not(None),Item.editor_id.is_not(None),Item.reviewer_id!=Item.editor_id,
        Page.reviewer_id.is_not(None),Page.editor_id.is_not(None),Page.reviewer_id!=Page.editor_id,
        Asset.status=='clean',Asset.version_id==Batch.version_id,
        or_(Item.annotation['category'].as_string().is_(None),Item.annotation['category'].as_string().in_(['person','human','人物','人'])),
        or_(Page.snapshot['dedup_excluded'].as_boolean().is_(None),Page.snapshot['dedup_excluded'].as_boolean().is_(False)),*clauses
    ).group_by(Batch.version_id).order_by(func.count(func.distinct(Item.asset_id)).desc(),Batch.version_id)
    from .evidence_access import truth_condition
    query=query.where(~truth_condition())
    items=[];refs=[]
    from .assistant_tools import version_info
    for version_id,count in db.execute(query):
        version=db.get(Version,version_id)
        if predicate and not predicate(version):continue
        if not can_read_content(db,user,version,settings):continue
        items.append({**version_info(db,user,settings,version_id,False),'content_evidence':True,
            'matched_images':int(count),'match_basis':'已独立复核的同一目标标签；不是仅依据数据集名称'})
        refs.append({'version_id':version_id,'content':True,'scope':'content:read'})
        if len(items)>=limit:break
    return items,refs
