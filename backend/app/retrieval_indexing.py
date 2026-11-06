"""【学习标注：索引 Worker】领取小事件 → 事务外读文件/编码/写索引 → CAS 提交。

向量先写，SQL 后发布；读取只信 SQL 中当前有效的检索单元。旧 Worker 只能
留下不可见孤儿向量，不能把旧知识或旧图片重新发布。失败指数退避，可人工重试。
"""
import hashlib
import json
import re
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update

from .db import now, uid
from .models import Asset, Dataset, KnowledgeDocument, RetrievalEntry, RetrievalEvent, Version
from .retrieval_events import enqueue
from .retrieval_providers import HTTPEncoder, QdrantIndex, RetrievalUnavailable


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def catalog_text(ds, version):
    # card 下划线字段、审核人、评测真值、任务载荷等私有元数据禁止进入语义索引。
    allowed = {k: version.card[k] for k in ("task", "description", "split", "annotation", "scenes") if k in version.card}
    return json.dumps({"name": ds.name, "description": ds.description[:12000], "tags": ds.tags,
                       "category": ds.category, "modality": ds.modality, "license": version.license,
                       "source": version.source[:4000], "card": allowed}, ensure_ascii=False, sort_keys=True)[:20000]


def source_snapshot(db, kind, source_id):
    """授权展示前也计算同一指纹；尚未更新的旧索引不能继续提供过期正文。"""
    if kind == "knowledge":
        doc = db.get(KnowledgeDocument, source_id)
        if not doc or doc.status != "published":
            return None
        return {"title": doc.title, "body": doc.body, "space_id": doc.space_id,
                "version_id": None, "source_revision": doc.revision,
                "fingerprint": digest([doc.title, doc.body, doc.revision, doc.space_id])}
    asset = db.get(Asset, source_id) if kind == "image" else None
    version = db.get(Version, asset.version_id if asset else source_id)
    ds = db.get(Dataset, version.dataset_id) if version else None
    if not version or not ds or ds.status != "active":
        return None
    if kind == "image":
        if not asset or asset.status != "clean" or not asset.media_type.startswith("image/"):
            return None
        from .evidence_access import is_truth
        if is_truth(db, asset):
            return None
        return {"title": asset.filename, "body": "", "version_id": version.id, "space_id": ds.space_id,
                "source_revision": 0, "fingerprint": digest([asset.sha256, asset.size, asset.media_type]),
                "object_key": asset.object_key, "size": asset.size, "sha256": asset.sha256}
    body = catalog_text(ds, version)
    return {"title": ds.name + f" · v{version.number}", "body": body, "version_id": version.id,
            "space_id": ds.space_id, "source_revision": 0, "fingerprint": digest(body)}


def split_document(text, max_chars=1000):
    """按 Markdown 标题分节；段落过长继续拆，保留小幅重叠和原文段落。"""
    section, buffer = "正文", ""
    output = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if paragraph.startswith("#"):
            if buffer:
                output.append((section, buffer))
                buffer = ""
            section = paragraph.splitlines()[0].lstrip("# ")[:200] or "正文"
        if buffer and len(buffer) + len(paragraph) > max_chars:
            output.append((section, buffer))
            buffer = ""
        while len(paragraph) > max_chars:
            output.append((section, paragraph[:max_chars]))
            paragraph = paragraph[max_chars-100:]
        buffer = (buffer + "\n\n" + paragraph).strip()
    if buffer:
        output.append((section, buffer))
    return output


def eligible():
    return and_(RetrievalEvent.kind.in_(["knowledge", "catalog", "image", "version_images"]),
        or_(and_(RetrievalEvent.status == "queued", RetrievalEvent.available_at <= now()),
            and_(RetrievalEvent.status == "running", RetrievalEvent.lease_until < now())))


def process_one(database, storage, settings, event_id, encoder=None, index=None):
    encoder, index = encoder or HTTPEncoder(settings), index or QdrantIndex(settings)
    token = uid()
    with database.Session() as db:
        claimed = db.execute(update(RetrievalEvent).where(RetrievalEvent.id == event_id, eligible()).values(
            status="running", lease_token=token, lease_until=now()+timedelta(seconds=settings.retrieval_lease_seconds),
            attempts=RetrievalEvent.attempts+1))
        if claimed.rowcount != 1:
            return
        row = db.get(RetrievalEvent, event_id)
        kind, source_id, generation, cursor = row.kind, row.source_id, row.generation, row.cursor
        db.commit()
    new_ids, published = [], False
    try:
        with database.Session() as db:
            if kind == "version_images":
                version = db.get(Version, source_id)
                ds = db.get(Dataset, version.dataset_id) if version else None
                assets = list(db.scalars(select(Asset.id).where(Asset.version_id == source_id,
                    Asset.id > cursor, Asset.status == "clean", Asset.media_type.like("image/%"))
                    .order_by(Asset.id).limit(200))) if ds and ds.status == "active" else []
                event = db.scalar(select(RetrievalEvent).where(RetrievalEvent.id == event_id,
                    RetrievalEvent.lease_token == token, RetrievalEvent.generation == generation).with_for_update())
                if event:
                    for asset_id in assets:
                        enqueue(db.connection(), "image", asset_id)
                    event.cursor = assets[-1] if assets else cursor
                    event.processed += len(assets)
                    event.status = "queued" if len(assets) == 200 else "done"
                    event.lease_token, event.lease_until = None, None
                    event.available_at = now()
                    db.commit()
                return
            snapshot = source_snapshot(db, kind, source_id)
        entries, points = [], []
        model = settings.retrieval_image_model if kind == "image" else settings.retrieval_text_model
        if snapshot:
            if kind == "image":
                if snapshot["size"] > settings.retrieval_max_image_bytes:
                    raise RetrievalUnavailable("image_exceeds_encoder_limit")
                raw = b"".join(storage.chunks(snapshot["object_key"], 0, snapshot["size"]-1))
                if len(raw) != snapshot["size"] or hashlib.sha256(raw).hexdigest() != snapshot["sha256"]:
                    raise RetrievalUnavailable("image_integrity_failed")
                parts, inputs = [("图片", "")], [raw]
            else:
                parts = split_document(snapshot["body"])
                inputs = [snapshot["title"] + "\n" + section + "\n" + body for section, body in parts]
            # 每次编码最多8段；每批续租，整个文档不占一个无限时外部请求。
            for start in range(0, len(inputs), 8):
                with database.Session() as db:
                    alive = db.execute(update(RetrievalEvent).where(RetrievalEvent.id == event_id,
                        RetrievalEvent.lease_token == token, RetrievalEvent.generation == generation).values(
                            lease_until=now()+timedelta(seconds=settings.retrieval_lease_seconds))).rowcount
                    db.commit()
                if not alive:
                    raise RetrievalUnavailable("index_superseded")
                vectors = encoder.encode(kind, inputs[start:start+8])
                for offset, vector in enumerate(vectors):
                    section, body = parts[start+offset]
                    identifier = uid()
                    entry = {k: snapshot[k] for k in ("title", "version_id", "space_id", "source_revision", "fingerprint")}
                    entries.append(dict(entry, id=identifier, kind=kind, source_id=source_id,
                                        model_revision=model, section=section, body=body))
                    points.append({"id": identifier, "vector": vector, "payload": {
                        "source_id": source_id, "version_id": snapshot["version_id"] or "",
                        "space_id": snapshot["space_id"] or "platform", "created_at": now().isoformat()}})
            new_ids = [entry["id"] for entry in entries]
            # 多个批次写入有界向量请求。
            for start in range(0, len(points), 16):
                index.upsert(kind, points[start:start+16])
        with database.Session() as db:
            event = db.scalar(select(RetrievalEvent).where(RetrievalEvent.id == event_id,
                RetrievalEvent.lease_token == token, RetrievalEvent.generation == generation).with_for_update())
            if not event:
                raise RetrievalUnavailable("index_superseded")
            current = source_snapshot(db, kind, source_id)
            if (current or {}).get("fingerprint") != (snapshot or {}).get("fingerprint"):
                raise RetrievalUnavailable("source_changed")
            old = list(db.scalars(select(RetrievalEntry).where(RetrievalEntry.kind == kind, RetrievalEntry.source_id == source_id)))
            old_ids = [entry.id for entry in old]
            for entry in old:
                db.delete(entry)
            db.add_all([RetrievalEntry(**entry) for entry in entries])
            event.status, event.lease_token, event.lease_until, event.error_code = "done", None, None, None
            event.processed = len(entries)
            db.commit()
            published = True
        try:
            index.delete(kind, old_ids)
        except RetrievalUnavailable:
            pass  # SQL 已发布，旧向量不可读；独立对账清除孤儿。
    except Exception as exc:
        if published:
            return  # 已提交的有效点不能因后续垃圾清理失败而删除。
        if not isinstance(exc, RetrievalUnavailable):
            from .diagnostics import report
            report(logging.getLogger(__name__), 'retrieval_index_failed', exc, operation=kind)
        try:
            index.delete(kind, new_ids)
        except Exception:
            pass
        with database.Session() as db:
            event = db.scalar(select(RetrievalEvent).where(RetrievalEvent.id == event_id,
                RetrievalEvent.lease_token == token, RetrievalEvent.generation == generation).with_for_update())
            if event:
                event.status = "failed" if event.attempts >= settings.retrieval_max_attempts else "queued"
                event.available_at = now()+timedelta(seconds=min(900, 2**min(event.attempts, 9)))
                event.error_code = str(exc)[:80] if isinstance(exc, RetrievalUnavailable) else "index_processing_failed"
                event.lease_token, event.lease_until = None, None
                db.commit()


def process_batch(database, storage, settings, encoder=None, index=None):
    if not settings.retrieval_enabled:
        return
    with database.Session() as db:
        identifiers = list(db.scalars(select(RetrievalEvent.id).where(eligible()).order_by(
            RetrievalEvent.available_at, RetrievalEvent.id).limit(settings.retrieval_index_batch)))
    for identifier in identifiers:
        process_one(database, storage, settings, identifier, encoder, index)


def reconcile_orphans(database, settings, index=None):
    """游标遍历派生向量，清理崩溃留下且不在SQL中的孤儿；不删除原图/业务文件。"""
    if not settings.retrieval_enabled:
        return
    index = index or QdrantIndex(settings)
    for kind in ("knowledge", "catalog", "image"):
        source = digest(index.collection(kind))[:32]
        with database.Session() as db:
            marker = db.scalar(select(RetrievalEvent).where(RetrievalEvent.kind == "gc", RetrievalEvent.source_id == source))
            if not marker:
                enqueue(db.connection(), "gc", source)
                marker = db.scalar(select(RetrievalEvent).where(RetrievalEvent.kind == "gc", RetrievalEvent.source_id == source))
                marker.status = "maintenance"
                db.commit()
            offset = marker.cursor
        try:
            points, next_offset = index.scroll(kind, offset or None)
            ids = [str(p["id"]).replace("-", "") for p in points]
            with database.Session() as db:
                live = set(db.scalars(select(RetrievalEntry.id).where(RetrievalEntry.id.in_(ids))))
            cutoff = now()-timedelta(seconds=settings.retrieval_lease_seconds+300)
            garbage = []
            for p, identifier in zip(points, ids):
                created = p.get("payload", {}).get("created_at")
                if identifier not in live and created and datetime.fromisoformat(created).replace(tzinfo=None) < cutoff:
                    garbage.append(identifier)
            index.delete(kind, garbage)
            with database.Session() as db:
                db.execute(update(RetrievalEvent).where(RetrievalEvent.kind == "gc", RetrievalEvent.source_id == source,
                    RetrievalEvent.cursor == offset).values(cursor=str(next_offset).replace("-", "") if next_offset else ""))
                db.commit()
        except (RetrievalUnavailable, ValueError, TypeError):
            continue
