"""【学习标注：外部适配器】有界 HTTP，不跟随跳转，不把响应/凭据写进错误。

API 与索引 Worker 使用同一协议。测试注入适配器，生产不提供伪向量兜底。
"""
import base64
import hashlib
import json
import math
from uuid import UUID
from urllib.parse import urlsplit

import httpx


class RetrievalUnavailable(Exception):
    pass


def request_json(url, method, path, payload=None, key="", timeout=15):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RetrievalUnavailable("invalid_service_configuration")
    try:
        budget = httpx.Timeout(timeout, connect=min(timeout, 3), pool=1, write=min(timeout, 5))
        with httpx.Client(timeout=budget, follow_redirects=False, trust_env=False) as client:
            headers = {"api-key": key, "Authorization": f"Bearer {key}"} if key else {}
            with client.stream(method, url.rstrip("/") + path, json=payload, headers=headers) as response:
                if response.status_code == 404:
                    return None
                if not 200 <= response.status_code < 300:
                    raise RetrievalUnavailable("service_rejected")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 4*1024**2:
                        raise RetrievalUnavailable("service_response_too_large")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
    except RetrievalUnavailable:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        raise RetrievalUnavailable("service_unavailable") from None


def normalize(vector, dimensions):
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise RetrievalUnavailable("embedding_dimensions_mismatch")
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
            raise ValueError()
    except (ValueError, OverflowError):
        raise RetrievalUnavailable("invalid_embedding") from None
    norm = math.sqrt(sum(v*v for v in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise RetrievalUnavailable("empty_embedding")
    return [v/norm for v in vector]


class HTTPEncoder:
    def __init__(self, settings):
        self.settings = settings

    def encode(self, kind, inputs):
        cfg = self.settings
        model = cfg.retrieval_image_model if kind == "image" else cfg.retrieval_text_model
        dimensions = cfg.retrieval_image_dimensions if kind == "image" else cfg.retrieval_text_dimensions
        payload = {"model": model, "input": [base64.b64encode(i).decode() for i in inputs] if kind == "image" else inputs}
        result = request_json(cfg.retrieval_encoder_url, "POST", "/image-embeddings" if kind == "image" else "/embeddings",
                              payload, cfg.retrieval_encoder_key, cfg.retrieval_timeout_seconds)
        if not isinstance(result, dict) or result.get("model") != model:
            raise RetrievalUnavailable("embedding_model_mismatch")
        rows = result.get("data", [])
        if not isinstance(rows, list) or len(rows) != len(inputs) or any(not isinstance(row, dict) or row.get("index") != i for i, row in enumerate(rows)):
            raise RetrievalUnavailable("embedding_count_mismatch")
        return [normalize(row.get("embedding"), dimensions) for row in rows]

    def rerank(self, question, passages):
        cfg = self.settings
        result = request_json(cfg.retrieval_encoder_url, "POST", "/rerank",
                              {"query": question, "documents": passages}, cfg.retrieval_encoder_key,
                              cfg.retrieval_timeout_seconds)
        scores = result.get("scores", []) if isinstance(result, dict) else []
        if not isinstance(scores, list) or len(scores) != len(passages) or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in scores):
            raise RetrievalUnavailable("invalid_reranker_response")
        return scores


class QdrantIndex:
    def __init__(self, settings):
        self.settings = settings

    def collection(self, kind):
        cfg = self.settings
        model = cfg.retrieval_image_model if kind == "image" else cfg.retrieval_text_model
        dims = cfg.retrieval_image_dimensions if kind == "image" else cfg.retrieval_text_dimensions
        # 模型或维度变化必须去新集合；不把不兼容向量混在一起。
        return f"dp_{kind}_{hashlib.sha256(f'{model}:{dims}'.encode()).hexdigest()[:16]}"

    def call(self, kind, method, suffix="", body=None):
        cfg = self.settings
        return request_json(cfg.retrieval_qdrant_url, method, "/collections/" + self.collection(kind) + suffix,
                            body, cfg.retrieval_qdrant_key, cfg.retrieval_timeout_seconds)

    def ensure(self, kind):
        dims = self.settings.retrieval_image_dimensions if kind == "image" else self.settings.retrieval_text_dimensions
        if self.call(kind, "GET") is None:
            try:
                self.call(kind, "PUT", body={"vectors": {"size": dims, "distance": "Cosine"},
                                            "on_disk_payload": True})
            except RetrievalUnavailable:
                if self.call(kind, "GET") is None:
                    raise
            for field in ("source_id", "version_id", "space_id"):
                self.call(kind, "PUT", "/index?wait=true", {"field_name": field, "field_schema": "keyword"})

    def upsert(self, kind, points):
        self.ensure(kind)
        self.call(kind, "PUT", "/points?wait=true", {"points": points})

    def delete(self, kind, identifiers):
        if identifiers:
            self.call(kind, "POST", "/points/delete?wait=true", {"points": identifiers})

    def search(self, kind, vector, scope, limit=60):
        # scope 只由服务端生成；客户端不能提交 Qdrant 过滤表达式。
        result = self.call(kind, "POST", "/points/search", {
            "vector": vector, "filter": scope, "limit": limit, "with_payload": False,
            "score_threshold": self.settings.retrieval_min_image_score if kind == "image" else self.settings.retrieval_min_text_score})
        rows = result.get("result", []) if isinstance(result, dict) else []
        try:
            if not isinstance(rows, list):
                raise ValueError()
            return [UUID(str(row["id"])).hex for row in rows[:limit]]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RetrievalUnavailable("invalid_vector_search_response") from None

    def scroll(self, kind, offset=None):
        payload = {"limit": 128, "with_payload": True, "with_vector": False}
        if offset:
            payload["offset"] = offset
        body = self.call(kind, "POST", "/points/scroll", payload)
        result = body.get("result", {}) if isinstance(body, dict) else {}
        return result.get("points", []), result.get("next_page_offset")


def providers(app):
    # 私有适配器注入只供隔离测试；HTTP 请求没有选择实现或 URL 的参数。
    return getattr(app.state, "retrieval_encoder", None) or HTTPEncoder(app.state.settings), \
        getattr(app.state, "retrieval_index", None) or QdrantIndex(app.state.settings)
