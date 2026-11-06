"""【学习标注：索引事务事件】业务事务提交，索引事件才可见；回滚一起消失。

不在请求中调用模型。SQL upsert 合并同一来源的多次变化，不建立大文件清单。
"""
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from .db import now, uid
from .models import Asset, Dataset, KnowledgeDocument, RetrievalEvent, Version


def enqueue(connection, kind, source_id):
    """跨 MySQL/SQLite 的原子 upsert；更新期间旧租约失效。"""
    table = RetrievalEvent.__table__
    values = dict(id=uid(), created_at=now(), kind=kind, source_id=source_id,
                  generation=1, status="queued", cursor="", processed=0, attempts=0,
                  available_at=now(), lease_until=None, lease_token=None, error_code=None)
    changes = {k: v for k, v in values.items() if k not in {"id", "created_at", "kind", "source_id", "generation"}}
    changes["generation"] = table.c.generation + 1
    if connection.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(table).values(**values).on_duplicate_key_update(**changes)
    else:
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(table).values(**values).on_conflict_do_update(
            index_elements=["kind", "source_id"], set_=changes)
    connection.execute(statement)


@event.listens_for(Session, "before_flush")
def remember_changes(session, *_):
    if not session.info.get("retrieval_enabled"):
        return
    watched = (Asset, Dataset, Version, KnowledgeDocument)
    changed = [obj for obj in session.new | session.deleted if isinstance(obj, watched)]
    for obj in session.dirty:
        if not isinstance(obj, watched):
            continue
        # 质量结果/checks、用量等变化不触发昂贵的图像向量提取。
        columns = {Asset: ("status", "sha256", "object_key", "filename", "media_type"),
                   Dataset: ("name", "description", "tags", "category", "status", "modality"),
                   Version: ("status", "license", "source", "card"),
                   KnowledgeDocument: ("title", "body", "status", "revision", "space_id")}[type(obj)]
        if any(inspect(obj).attrs[name].history.has_changes() for name in columns):
            changed.append(obj)
    session.info["retrieval_changes"] = changed


@event.listens_for(Session, "after_flush_postexec")
def write_changes(session, *_):
    changed = session.info.pop("retrieval_changes", [])
    if not changed:
        return
    connection = session.connection()
    sources = set()
    for obj in changed:
        if isinstance(obj, Dataset):
            sources.update(("catalog", identifier) for identifier in connection.scalars(
                select(Version.id).where(Version.dataset_id == obj.id)))
        else:
            sources.add(("image" if isinstance(obj, Asset) else
                         "catalog" if isinstance(obj, Version) else "knowledge", obj.id))
    for kind, identifier in sources:
        enqueue(connection, kind, identifier)


@event.listens_for(Session, "after_rollback")
def forget_changes(session):
    session.info.pop("retrieval_changes", None)
