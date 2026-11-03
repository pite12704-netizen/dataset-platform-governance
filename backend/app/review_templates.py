"""Version-bound object review schemas. Definitions are data, never executable tools."""
import hashlib
import json
import math
from typing import Literal
from fastapi import APIRouter,Depends,Request
from pydantic import Field,model_validator
from sqlalchemy import select
from .schemas import Input
from .common import audit,fetch,require
from .db import get_db
from .models import Dataset,Version,Job,ImageReviewBatch
from .policy import manage_dataset,can_view_version
from .security import interactive_user

router=APIRouter(prefix='/review-templates',tags=['自定义目标审核模板'])

class Category(Input):
    key:str=Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    label:str=Field(min_length=1,max_length=60)
    description:str=Field(default='',max_length=300)

class Attribute(Input):
    key:str=Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    label:str=Field(min_length=1,max_length=60)
    type:Literal['enum','boolean','number','text']='enum'
    options:list[str]=Field(default_factory=list,max_length=40)
    description:str=Field(default='',max_length=300)
    @model_validator(mode='after')
    def valid_options(self):
        require(self.type!='enum' or 1<=len(self.options)==len(set(self.options)),'枚举选项不能为空或重复',422)
        require(all(0<len(v)<=60 for v in self.options),'选项须为1–60字',422)
        require(self.type=='enum' or not self.options,'非枚举字段不能设置选项',422)
        return self

class Template(Input):
    schema_version:Literal[1]=1
    name:str=Field(min_length=1,max_length=80)
    classes:list[Category]=Field(min_length=1,max_length=30)
    fields:list[Attribute]=Field(default_factory=list,max_length=20)
    @model_validator(mode='after')
    def unique_keys(self):
        for rows in (self.classes,self.fields):
            require(len(rows)==len({r.key for r in rows}),'类别或字段标识不能重复',422)
        return self

def normalize(value):return Template.model_validate(value).model_dump()

def digest(template):
    return hashlib.sha256(json.dumps(normalize(template),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def for_version(version):
    value=version.card.get('_review_template')
    return normalize(value) if value else None

def for_batch(batch):return batch.provenance.get('review_template')

def check_annotation(value,template):
    template=normalize(template)
    require(value.get('category') in {c['key'] for c in template['classes']},'目标类别不在本批次模板中',422)
    labels=value['labels'];fields={f['key']:f for f in template['fields']}
    require(set(labels)==set(fields),'审核属性与本批次模板不一致',422)
    for key,field in fields.items():
        item=labels[key]
        if item is None:continue # Unknown/occluded is explicit, never silently guessed.
        valid=(item in field['options'] if field['type']=='enum' and isinstance(item,str) else
            type(item) is bool if field['type']=='boolean' else
            type(item) in {int,float} and math.isfinite(item) if field['type']=='number' else
            isinstance(item,str) and len(item)<=500 if field['type']=='text' else False)
        require(valid,'属性值不符合模板：'+key,422)
    return value

@router.get('/versions/{version_id}')
def read_template(version_id:str,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    version=fetch(db,Version,version_id)
    require(can_view_version(db,user,version,request.app.state.settings),'版本不可见',404)
    value=for_version(version)
    return {'template':value,'digest':digest(value) if value else None,'revision':version.card.get('_review_template_revision',0),'legacy':value is None}

@router.put('/versions/{version_id}')
def bind_template(version_id:str,data:Template,db=Depends(get_db),user=Depends(interactive_user)):
    from .workflows import locked_version
    version,ds=locked_version(db,version_id)
    require(manage_dataset(db,user,ds) and ds.status=='active','只有数据负责人可配置模板')
    require(version.status in {'draft','rejected'},'已冻结版本不能修改模板',409)
    require(version.quality_tier!='certified','当前自定义目标尚未接入独立认证评测，请在非认证版本配置模板',409)
    require(not db.scalar(select(Job.id).where(Job.version_id==version.id,Job.kind.in_(['vision','vision_plan'])).limit(1))
        and not db.scalar(select(ImageReviewBatch.id).where(ImageReviewBatch.version_id==version.id).limit(1)),
        '此版本已有识别或复核记录，请创建新版本绑定新模板；旧标注不自动转换',409)
    value=data.model_dump();previous=for_version(version);revision=version.card.get('_review_template_revision',0)+1
    version.card={**version.card,'_review_template':value,'_review_template_revision':revision}
    audit(db,user,'review_template.bind',version.id,ds.space_id,template_digest=digest(value),
        revision=revision,before=previous,after=value)
    db.commit();return {'template':value,'digest':digest(value),'revision':revision,'legacy':False}
