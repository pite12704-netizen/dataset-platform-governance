"""【学习标注：统一检索】授权范围 → 多路召回 → RRF → 二次鉴权 → 可追溯结果。

向量库只提供候选 ID，不提供可信正文/权限。外部 HTTP 阶段不持有 SQL 会话。
"""
import re

from sqlalchemy import and_, or_, select

from .common import fetch, require
from .models import Asset, Dataset, KnowledgeDocument, Membership, RetrievalEntry, Version
from .policy import can_read_content, can_view_version, member
from .retrieval_indexing import source_snapshot
from .retrieval_providers import RetrievalUnavailable, providers
from .security import current_user, enforce_scope, optional_user


def actor(request, db, kind):
    user = current_user(optional_user(request, db))
    enforce_scope(request, "content:read" if kind == "image" else "catalog:read")
    return user


def can_read_document(db, user, doc):
    return bool(doc and doc.status == "published" and (doc.space_id is None or member(db, user, doc.space_id)))


def terms(question):
    clean = question.lower()
    for word in ("帮我", "请问", "查找", "寻找", "数据集", "如何", "怎么", "平台知识", "推荐"):
        clean = clean.replace(word, " ")
    words = re.findall(r"[a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", clean)
    output = set(words)
    for word in words:
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            output.update(word[i:i+2] for i in range(len(word)-1))
    return sorted(output, key=lambda value: (-len(value), value))[:24]


def rrf(rankings, constant=60):
    scores = {}
    for ranking in rankings:
        for rank, identifier in enumerate(dict.fromkeys(ranking)):
            scores[identifier] = scores.get(identifier, 0) + 1/(constant+rank+1)
    return sorted(scores, key=lambda identifier: (-scores[identifier], identifier))


def scope_for(db, user, settings, kind, version_id=None, space_id=None, modality=None, version_ids=None):
    if kind == "knowledge":
        spaces = select(Membership.space_id).where(Membership.user_id == user.id, Membership.active.is_(True))
        query = select(KnowledgeDocument.id).where(KnowledgeDocument.status == "published",
            or_(KnowledgeDocument.space_id.is_(None), KnowledgeDocument.space_id.in_(spaces)))
        if space_id:
            query = query.where(KnowledgeDocument.space_id == space_id)
        identifiers = list(db.scalars(query.limit(settings.retrieval_scope_limit+1)))
        field = "source_id"
    elif kind == "image":
        version = fetch(db, Version, version_id)
        require(can_read_content(db, user, version, settings), "没有此版本的图片内容访问权限")
        identifiers, field = [version.id], "version_id"
    else:
        from .datasets import catalog_query
        datasets = catalog_query(db, user, settings, modality=modality).where(Dataset.status != 'blocked').order_by(None).with_only_columns(Dataset.id)
        if space_id:
            datasets = datasets.where(Dataset.space_id == space_id)
        owned_spaces = select(Membership.space_id).where(Membership.user_id == user.id, Membership.active.is_(True))
        owned = select(Dataset.id).where(Dataset.owner_id == user.id, Dataset.space_id.in_(owned_spaces))
        query = select(Version.id).where(Version.dataset_id.in_(datasets), or_(Version.status == "published", Version.dataset_id.in_(owned)))
        if version_id:
            query = query.where(Version.id == version_id)
        if version_ids is not None:
            query = query.where(Version.id.in_(version_ids))
        # 将版本范围下推 SQL，避免扫描几千个版本逐个鉴权；返回结果仍用策略二次检查。
        identifiers = list(db.scalars(query.order_by(Version.id).limit(settings.retrieval_scope_limit+1)))
        field = "version_id"
    require(len(identifiers) <= settings.retrieval_scope_limit, "检索范围过大，请选择组织或具体版本后查询", 413)
    return identifiers, field


def valid_entry(db, user, settings, entry, kind):
    model = settings.retrieval_image_model if kind == "image" else settings.retrieval_text_model
    if entry.kind != kind or entry.model_revision != model:
        return False
    if kind == "knowledge":
        doc = db.get(KnowledgeDocument, entry.source_id)
        if not can_read_document(db, user, doc):
            return False
    else:
        version = db.get(Version, entry.version_id)
        if not version or not can_view_version(db, user, version, settings):
            return False
        if kind == "image" and not can_read_content(db, user, version, settings):
            return False
    snapshot = source_snapshot(db, kind, entry.source_id)
    return bool(snapshot and snapshot["fingerprint"] == entry.fingerprint)


def check_references(db, user, settings, result):
    """模型返回、对话重放、历史读取都要使用；文档修订/撤权后隐藏旧回答。"""
    for citation in result.get("knowledge_citations", []):
        entry = db.get(RetrievalEntry, citation["entry_id"])
        require(entry and valid_entry(db, user, settings, entry, "knowledge"), "知识来源已修改或不可访问", 403)


def matches_filters(version, modality, license_name, commercial, db):
    ds = db.get(Dataset, version.dataset_id)
    return (not modality or ds.modality == modality) and (not license_name or version.license == license_name) and (
        not commercial or version.card.get("usage_policy", {}).get("commercial_use") == "allowed")


def search(app, request, kind, question="", *, version_id=None, space_id=None, modality=None,
           license_name=None, commercial=False, asset_id=None, raw_image=None, limit=8, concepts=None, excluded_modalities=None, version_ids=None):
    """同步函数在有界工作线程执行；每个数据库阶段都打开/关闭自己的短会话。"""
    cfg = app.state.settings
    require(kind in {"knowledge", "catalog", "image"}, "未知检索类型", 422)
    require(kind != "image" or version_id, "图片检索必须选择一个版本", 422)
    if kind == 'catalog' and not modality:
        aliases = {'image': ('图像数据', '图片数据'), 'text': ('文本数据', '语料'),
                   'audio': ('音频数据', '语音数据'), 'video': ('视频数据',), 'tabular': ('表格数据',)}
        matches = [name for name, words in aliases.items() if any(
            word in question and not any(prefix+word in question for prefix in ('不要', '不是', '排除', '不需要')) for word in words)]
        if len(matches) == 1:
            modality = matches[0]
    if commercial is False and kind == "catalog" and not any(word in question for word in ('不允许商用','不能商用','禁止商用')) and any(word in question for word in ("允许商用", "商业使用", "可以商用")):
        commercial = True
    encoder, index = providers(app)
    from .agent_query_memory import refine, evidence_matches
    from .retrieval_indexing import catalog_text
    concepts = list(concepts) if concepts is not None else refine(None, question, 'catalog')['concepts'] if kind=='catalog' else []
    with app.state.db.Session() as db:
        user = actor(request, db, kind)
        identifiers, field = scope_for(db, user, cfg, kind, version_id, space_id, modality, version_ids)
        model = cfg.retrieval_image_model if kind == "image" else cfg.retrieval_text_model
        query = select(RetrievalEntry).where(RetrievalEntry.kind == kind, getattr(RetrievalEntry, field).in_(identifiers),
                                             RetrievalEntry.model_revision == model)
        lexical = []
        if kind != "image":
            words = terms(question)
            if cfg.retrieval_lexical_mode=='bm25':
                from .bm25 import rank
                corpus=list(db.scalars(query.order_by(RetrievalEntry.id).limit(cfg.retrieval_bm25_max_entries+1)))
                require(len(corpus)<=cfg.retrieval_bm25_max_entries,'BM25语料超过查询预算，请选择组织或版本缩小范围',413)
                require(sum(len((r.title+r.body).encode()) for r in corpus)<=cfg.retrieval_bm25_max_bytes,
                        'BM25语料字节数超过查询预算，请缩小范围',413)
                lexical=[identifier for identifier,score in rank(question,corpus)]
            elif words:
                clauses = []
                for word in words:
                    pattern = "%"+word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")+"%"
                    clauses.append(or_(RetrievalEntry.title.ilike(pattern, escape="\\"), RetrievalEntry.body.ilike(pattern, escape="\\")))
                # 受限候选词法召回；不是全库 BM25，返回诊断明确候选上限。
                rows = db.scalars(query.where(or_(*clauses)).order_by(RetrievalEntry.id).limit(120)).all()
                lexical = [row.id for row in sorted(rows, key=lambda r: (
                    -sum((r.title.lower().count(w)*3+r.body.lower().count(w))*len(w) for w in words), r.id))]
        raw = raw_image
        if asset_id:
            asset = fetch(db, Asset, asset_id)
            require(asset.version_id == version_id, "查询图片不属于所选版本", 404)
            require(source_snapshot(db, "image", asset.id), "查询图片检查未通过或属于受保护评测真值", 403)
            asset_info = (asset.object_key, asset.size, asset.sha256)
            require(asset.size <= cfg.retrieval_max_image_bytes, "查询图片超过编码限制", 413)
        else:
            asset_info = None
    if not identifiers:
        return {"items": [], "mode": "empty-scope", "degraded": False, "limit": limit}
    vector_ids, degraded, reranked = [], False, False
    if cfg.retrieval_enabled:
        try:
            if asset_info:
                import hashlib
                raw = b"".join(app.state.storage.chunks(asset_info[0], 0, asset_info[1]-1))
                if len(raw) != asset_info[1] or hashlib.sha256(raw).hexdigest() != asset_info[2]:
                    raise RetrievalUnavailable("query_image_integrity_failed")
            if kind == "image":
                require(raw and len(raw) <= cfg.retrieval_max_image_bytes, "请选择查询图片", 422)
            vector = encoder.encode(kind, [raw if kind == "image" else question])[0]
            vector_ids = index.search(kind, vector, {"must": [{"key": field, "match": {"any": identifiers}}]}, 80)
        except RetrievalUnavailable:
            degraded = True
    elif kind == "image":
        raise RetrievalUnavailable("image_retrieval_disabled")
    if kind == "image" and degraded:
        raise RetrievalUnavailable("image_retrieval_unavailable")
    ranking = rrf([lexical, vector_ids])
    # 重排前先鉴权和核对来源；未授权文本不能送往编码/重排服务。
    with app.state.db.Session() as db:
        user = actor(request, db, kind)
        current_ids, _ = scope_for(db, user, cfg, kind, version_id, space_id, modality, version_ids)
        rows = {row.id: row for row in db.scalars(select(RetrievalEntry).where(RetrievalEntry.id.in_(ranking[:160]),
            getattr(RetrievalEntry, field).in_(current_ids)))}
        ranking = [key for key in ranking if key in rows and valid_entry(db, user, cfg, rows[key], kind)]
        passages = [rows[key].title+"\n"+rows[key].body[:1800] for key in ranking[:20]]
    if cfg.retrieval_rerank_enabled and kind != "image" and passages:
        try:
            scores = encoder.rerank(question, passages)
            head = sorted(zip(ranking[:20], scores), key=lambda item: -item[1])
            ranking = [key for key, _ in head] + ranking[20:]
            reranked = True
        except RetrievalUnavailable:
            degraded = True
    items = []
    content_refs=[]
    with app.state.db.Session() as db:
        user = actor(request, db, kind)
        for key in ranking:
            row = db.get(RetrievalEntry, key)
            if not row or not valid_entry(db, user, cfg, row, kind):
                continue
            if kind == "knowledge":
                items.append({"entry_id": row.id, "document_id": row.source_id, "name": row.title,
                    "section": row.section, "revision": row.source_revision, "excerpt": row.body,
                    "url": f"/retrieval?document={row.source_id}"})
            elif kind == "catalog":
                from .assistant_tools import version_info
                version = db.get(Version, row.version_id)
                if db.get(Dataset,version.dataset_id).modality in (excluded_modalities or []):
                    continue
                if not matches_filters(version, modality, license_name, commercial, db):
                    continue
                if not evidence_matches(catalog_text(db.get(Dataset,version.dataset_id),version), concepts):
                    continue
                if any(item.get('version_id') == version.id for item in items):
                    continue
                info = version_info(db, user, cfg, version.id, False)
                items.append({**info, "entry_id": row.id, "match_basis": "数据集及版本元数据声明；不代表已完成人工场景核验"})
            else:
                if row.source_id == asset_id:
                    continue
                items.append({"entry_id": row.id, "asset_id": row.source_id, "version_id": row.version_id,
                    "name": row.title, "preview_url": f"/api/v1/retrieval/images/{row.source_id}/preview"})
            if len(items) >= limit:
                break
        # 无向量或索引未覆盖时，沿用原始目录关键词查询，并执行相同硬条件。
        if kind == "catalog" and len(items) < limit:
            from .assistant_tools import search as keyword_search, version_info
            for candidate in keyword_search(db, user, cfg, question, limit=100):
                vid = candidate.get("latest_version_id")
                if not vid or vid not in current_ids or any(i["version_id"] == vid for i in items):
                    continue
                version = db.get(Version, vid)
                if db.get(Dataset,version.dataset_id).modality in (excluded_modalities or []):
                    continue
                if not matches_filters(version, modality, license_name, commercial, db):
                    continue
                if not evidence_matches(catalog_text(db.get(Dataset,version.dataset_id),version), concepts):
                    continue
                items.append({**version_info(db, user, cfg, vid, False), "match_basis": "实时关键词目录"})
                if len(items) >= limit:
                    break
        if kind=='catalog' and concepts and (getattr(request.state,'scopes',None) is None or 'content:read' in request.state.scopes):
            from .annotation_search import candidates
            checked,content_refs=candidates(db,user,cfg,current_ids,concepts,limit,
                lambda v:matches_filters(v,modality,license_name,commercial,db) and
                db.get(Dataset,v.dataset_id).modality not in (excluded_modalities or []))
            needs_verified = 'no_hat' in concepts or len(concepts)>1
            items=(checked if needs_verified else [*checked,*[i for i in items if i['version_id'] not in {c['version_id'] for c in checked}]])[:limit]
        elif kind=='catalog' and ('no_hat' in concepts or len(concepts)>1):
            items=[]
    return {"items": items, "mode": "hybrid" if vector_ids else "keyword", "degraded": degraded,
            "access_requirements":content_refs,"requires_verified_labels":kind=='catalog' and ('no_hat' in concepts or len(concepts)>1),
            "reranked": reranked, "limit": limit, "lexical_candidate_limit": 120,
            "embedding_model": model, "vector_enabled": cfg.retrieval_enabled,
            "lexical_method": cfg.retrieval_lexical_mode,
            "required_concepts": concepts, "evidence_policy": "metadata_mentions_not_image_verification",
            "applied_filters": {"modality": modality, "license": license_name, "commercial_use_declared": commercial},
            "similarity_is_duplicate_probability": False}


def grounded_answer(result):
    citations = result["items"][:5]
    if not citations:
        return {"answer": "当前可访问的知识资料中没有找到有效依据，请补充问题或联系管理员维护知识库。",
                "knowledge_citations": [], "citations": [], "mode": "knowledge-no-evidence", "status": "completed"}
    answer = "以下为当前有效资料中的原文依据：\n" + "\n\n".join(
        f"[{i+1}] {c['name']} · {c['section']}（修订 {c['revision']}）\n{c['excerpt'][:1000]}" for i, c in enumerate(citations))
    return {"answer": answer, "knowledge_citations": citations, "citations": [],
            "mode": "knowledge-extractive", "status": "degraded" if result["degraded"] else "completed",
            "retrieval_meta": {key: result.get(key) for key in ('mode', 'embedding_model', 'reranked', 'degraded', 'vector_enabled','lexical_method')}}
