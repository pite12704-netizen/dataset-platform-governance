"""Local, bounded embedding/reranking service. No remote model downloads or user paths.

Run uvicorn services.retrieval_encoder.server:app --host 127.0.0.1 --port 8091.
Configure local safetensors model directories and a service secret through environment.
This optional service has its own Python environment; importing platform API never loads Torch.
"""
import base64
import hmac
import io
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
gate = threading.BoundedSemaphore(1)


def secret():
    path = os.environ.get("ENCODER_API_KEY_FILE")
    if path:
        with open(path, encoding="utf-8") as source:
            return source.read().strip()
    return os.environ.get("ENCODER_API_KEY", "")


def authenticate(authorization: str = Header(default="")):
    key = secret()
    if not key or not hmac.compare_digest(authorization, "Bearer " + key):
        raise HTTPException(401, "Encoder authentication required")


@asynccontextmanager
async def lifespan(app):
    if not secret():
        raise RuntimeError("Configure encoder service authentication")
    from sentence_transformers import SentenceTransformer, CrossEncoder
    from transformers import ChineseCLIPModel, ChineseCLIPProcessor
    import torch
    app.state.torch = torch
    device = os.environ.get("ENCODER_DEVICE", "cpu")
    app.state.text = SentenceTransformer(os.environ["ENCODER_TEXT_PATH"], device=device,
        local_files_only=True, trust_remote_code=False, model_kwargs={"use_safetensors": True})
    app.state.text.max_seq_length = 2048
    app.state.image = ChineseCLIPModel.from_pretrained(os.environ["ENCODER_IMAGE_PATH"],
        local_files_only=True, use_safetensors=True).to(device).eval()
    app.state.processor = ChineseCLIPProcessor.from_pretrained(os.environ["ENCODER_IMAGE_PATH"], local_files_only=True, use_fast=False)
    app.state.device = device
    rerank = os.environ.get("ENCODER_RERANK_PATH")
    app.state.rerank = CrossEncoder(rerank, device=device, max_length=1024, trust_remote_code=False,
        local_files_only=True, model_kwargs={"use_safetensors": True}) if rerank else None
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class BoundRequests:
    """Authenticate and cap bytes before Pydantic materializes a JSON/base64 body."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') == '/health':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        key = secret()
        if not key or not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer '+key).encode()):
            return await JSONResponse({'detail': 'Encoder authentication required'}, 401)(scope, receive, send)
        limit, received = 30*1024**2, 0
        try:
            if int(headers.get(b'content-length', b'0')) > limit:
                return await JSONResponse({'detail': 'Request exceeds limit'}, 413)(scope, receive, send)
        except ValueError:
            return await JSONResponse({'detail': 'Invalid length'}, 400)(scope, receive, send)
        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get('body', b''))
            if received > limit:
                raise HTTPException(413, 'Request exceeds limit')
            return message
        await self.app(scope, bounded_receive, send)


app.add_middleware(BoundRequests)


class Embeddings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(max_length=128)
    input: list[str] = Field(min_length=1, max_length=8)


class Rerank(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    documents: list[str] = Field(min_length=1, max_length=20)


def enter():
    if not gate.acquire(blocking=False):
        raise HTTPException(503, "Encoder is busy", headers={"Retry-After": "2"})


@app.post("/embeddings", dependencies=[Depends(authenticate)])
def text_embeddings(data: Embeddings):
    if data.model != os.environ.get("ENCODER_TEXT_REVISION", "bge-m3-local-v1") or any(len(s)>22000 for s in data.input):
        raise HTTPException(422, "Invalid text encoding request")
    enter()
    try:
        vectors = app.state.text.encode(data.input, batch_size=8, normalize_embeddings=True, convert_to_numpy=True).tolist()
        return {"model": data.model, "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
    finally:
        gate.release()


@app.post("/image-embeddings", dependencies=[Depends(authenticate)])
def image_embeddings(data: Embeddings):
    if data.model != os.environ.get("ENCODER_IMAGE_REVISION", "chinese-clip-local-v1") or len(data.input)>1:
        raise HTTPException(422, "Invalid image encoding request")
    if len(data.input[0]) > 28*1024**2:
        raise HTTPException(413, "Image is too large")
    enter()
    image = None
    try:
        from PIL import Image, ImageOps
        raw = base64.b64decode(data.input[0], validate=True)
        with Image.open(io.BytesIO(raw)) as original:
            if original.width*original.height > 25000000 or original.format not in {"JPEG", "PNG", "WEBP", "BMP"}:
                raise HTTPException(422, "Unsupported image")
            image = ImageOps.exif_transpose(original).convert("RGB")
        inputs = app.state.processor(images=image, return_tensors="pt").to(app.state.device)
        with app.state.torch.inference_mode():
            vectors = app.state.image.get_image_features(**inputs)
            # Transformers 5 returns a structured output; earlier tensor-only responses
            # remain supported for protocol tests and controlled alternative providers.
            if hasattr(vectors, 'pooler_output'):
                vectors = vectors.pooler_output
            vectors = vectors / vectors.norm(p=2, dim=-1, keepdim=True)
        return {"model": data.model, "data": [{"index": 0, "embedding": vectors[0].cpu().tolist()}]}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Image decoding or encoding failed") from None
    finally:
        if image is not None:
            image.close()
        gate.release()


@app.post("/rerank", dependencies=[Depends(authenticate)])
def rerank(data: Rerank):
    if app.state.rerank is None:
        raise HTTPException(503, "Reranker is not configured")
    if any(len(s)>2200 for s in data.documents):
        raise HTTPException(413, "Passage exceeds limit")
    enter()
    try:
        scores = app.state.rerank.predict([(data.query, doc) for doc in data.documents], batch_size=4)
        return {"scores": scores.tolist()}
    finally:
        gate.release()


@app.get("/health")
def health():
    return {"status": "ready", "models_loaded": hasattr(app.state, "image")}
