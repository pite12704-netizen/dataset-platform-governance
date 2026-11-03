"""【学习标注：冷热分离】大字段不可变存储；热任务行的每个 JSON 字段最多 8KiB。"""
import base64
import hashlib
import json
import zlib
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.orm import attributes,object_session

from .db import now,uid
from .models import Job,JobPayload

REFS='_payload_refs_v1'
AUTH='_payload_auth_evaluation_set_id'
HOT_BYTES=8192
INLINE_BYTES=2048
RAW_BYTES=128*1024**2
ENCODED_BYTES=24*1024**2


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def encode(job_id,field,key,value,created_at=None):
    raw=canonical(value)
    if len(raw)>RAW_BYTES:raise HTTPException(413,'任务载荷超过单字段容量，请分批处理')
    digest=hashlib.sha256(raw).hexdigest()
    content=base64.b64encode(zlib.compress(raw,6)).decode('ascii')
    if len(content)>ENCODED_BYTES:raise HTTPException(413,'任务载荷编码后超过数据库写入容量，请分批处理')
    identifier=hashlib.sha256(canonical([job_id,field,key,digest])).hexdigest()[:32]
    row={'id':identifier,'created_at':created_at or now(),'job_id':job_id,'field':field,'key':key,
         'sha256':digest,'raw_bytes':len(raw),'content':content}
    return {'id':identifier,'sha256':digest,'bytes':len(raw)},row


def unpack(row,job_id,field,key,reference):
    if (not isinstance(reference,dict) or not isinstance(reference.get('id'),str) or len(reference['id'])!=32
            or not isinstance(reference.get('sha256'),str) or len(reference['sha256'])!=64
            or type(reference.get('bytes')) is not int):
        raise HTTPException(409,'任务冷载荷引用格式无效')
    if (row is None or row.job_id!=job_id or row.field!=field or row.key!=key or row.sha256!=reference.get('sha256')
            or row.raw_bytes!=reference.get('bytes') or not 0<=row.raw_bytes<=RAW_BYTES
            or not isinstance(row.content,str) or len(row.content)>ENCODED_BYTES):
        raise HTTPException(409,'任务冷载荷引用不一致或超出容量')
    try:
        decoder=zlib.decompressobj()
        raw=decoder.decompress(base64.b64decode(row.content,validate=True),row.raw_bytes+1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw)!=row.raw_bytes:
            raise ValueError('invalid compressed payload')
        if hashlib.sha256(raw).hexdigest()!=row.sha256:raise ValueError('checksum mismatch')
        return json.loads(raw)
    except (ValueError,zlib.error) as exc:
        raise HTTPException(409,'任务冷载荷完整性校验失败') from exc


def split(job_id,field,value,created_at=None):
    """纯转换也用于旧备份升级；每个大键独立存储，控制字段变化不重写大清单。"""
    if not isinstance(value,dict):raise HTTPException(422,'任务参数和结果必须为对象')
    hot=dict(value);existing=hot.pop(REFS,{})
    if not isinstance(existing,dict):raise HTTPException(409,'任务冷载荷索引格式无效')
    references=dict(existing);records=[]
    options=hot.get('governance_options') if field=='parameters' else None
    for key,item in list(hot.items()):
        if key in references:references.pop(key) # 明确赋新值时更新引用，而不是保留旧内容。
        if len(canonical(item))>INLINE_BYTES:
            if len(key)>100:raise HTTPException(422,'任务载荷字段名过长')
            reference,record=encode(job_id,field,key,item,created_at)
            references[key]=reference;records.append(record);hot.pop(key)
    if field=='parameters' and isinstance(options,dict):
        if 'governance_options' in references and options.get('evaluation_set_id'):hot[AUTH]=options['evaluation_set_id']
        else:hot.pop(AUTH,None)
    if references:hot[REFS]=references
    if len(canonical(hot))>HOT_BYTES:
        # 字段数量也是有界的，避免许多小字段绕过热行保护。
        raise HTTPException(413,'任务热状态超过容量，请减少参数字段或分批处理')
    return hot,records


def hydrate_job(session,job,fields=('parameters','result')):
    """只为需要正文的代码显式还原；set_committed_value 不使只读请求产生写操作。"""
    if not isinstance(job,Job):return job
    for field in fields:
        value=getattr(job,field)
        if not isinstance(value,dict) or REFS not in value:continue
        if not isinstance(value[REFS],dict):raise HTTPException(409,'任务冷载荷索引格式无效')
        resolved={key:item for key,item in value.items() if key not in {REFS,AUTH}}
        for key,reference in value[REFS].items():
            if not isinstance(reference,dict):raise HTTPException(409,'任务冷载荷引用格式无效')
            row=session.get(JobPayload,reference.get('id'))
            resolved[key]=unpack(row,job.id,field,key,reference)
        attributes.set_committed_value(job,field,resolved)
    return job


def compact_pending(session):
    """所有 ORM 任务写入前收缩；也覆盖旧任务的首次调度、重试或恢复。"""
    for record in session.dirty:
        if isinstance(record,JobPayload) and session.is_modified(record,include_collections=False):
            raise HTTPException(409,'任务冷载荷不可原地修改，请创建新的内容引用')
    for job in list(session.new)+list(session.dirty):
        if not isinstance(job,Job) or job in session.deleted:continue
        if not job.id:job.id=uid()
        for field in ('parameters','result'):
            value=getattr(job,field) or {}
            hot,records=split(job.id,field,value,job.created_at)
            for record in records:
                pending=next((row for row in session.new if isinstance(row,JobPayload) and row.id==record['id']),None)
                if pending is None and session.get(JobPayload,record['id']) is None:
                    session.add(JobPayload(**record,job=job))
            if hot!=value:setattr(job,field,hot)


def public_hydrate(job):
    session=object_session(job)
    if session is not None:return hydrate_job(session,job)
    if any(REFS in (getattr(job,field) or {}) for field in ('parameters','result')):
        raise HTTPException(503,'任务正文需在授权数据会话中读取')
    return job


def validate_backup_payloads(tables):
    """恢复前验证所有引用的归属、容量及正文，避免导入损坏/串任务引用。"""
    records={row['id']:SimpleNamespace(**row) for row in tables.get('job_payloads',[])}
    jobs={row['id']:row for row in tables.get('jobs',[])}
    for record in records.values():
        if record.job_id not in jobs:raise HTTPException(409,'备份存在无所属任务的冷载荷')
        unpack(record,record.job_id,record.field,record.key,
               {'id':record.id,'sha256':record.sha256,'bytes':record.raw_bytes})
    for job in jobs.values():
        for field in ('parameters','result'):
            value=job.get(field) or {}
            if len(canonical(value))>HOT_BYTES:raise HTTPException(409,'备份任务热状态尚未收缩')
            for key,reference in value.get(REFS,{}).items():
                unpack(records.get(reference.get('id')),job['id'],field,key,reference)
