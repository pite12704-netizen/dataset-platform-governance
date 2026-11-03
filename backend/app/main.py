"""【学习标注：应用入口】创建 FastAPI 应用，安装中间件、异常处理、路由和健康检查。

阅读顺序：create_app → include_router → /health/ready。业务规则不放在这里，
这里负责把配置、数据库、存储和各业务模块组装成一个可运行服务。
"""
import logging
import json
from contextlib import asynccontextmanager
from uuid import uuid4

from anyio import to_thread
from anyio import CapacityLimiter
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError as PoolTimeout
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .db import Base, Database
from .db import AsyncDatabase
from .middleware import RateLimiter, RequestGuardMiddleware
from .storage import make_storage


def create_app(settings: Settings | None = None, initialize=False):
    settings = settings or get_settings()
    # 内部 S3 签名 URL 属于凭据；不让第三方 HTTP INFO 日志输出完整请求地址。
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    database = Database(settings.database_url, settings)
    if initialize:
        if settings.environment != "test":
            raise ValueError("Schema auto-creation is only available in tests; use Alembic migrations")
        from . import models  # register metadata
        Base.metadata.create_all(database.engine)

    @asynccontextmanager
    async def lifespan(app):
        # 每个 API 进程的线程额度；数据库和模型额度需分别配置，不能无限扩线程。
        limiter = to_thread.current_default_thread_limiter()
        previous_tokens = limiter.total_tokens
        limiter.total_tokens = settings.api_thread_tokens
        app.state.thread_limiter = limiter
        from .async_model_gateway import ModelGateway
        app.state.model_gateway = ModelGateway(settings)
        app.state.http_metrics.start()
        await app.state.async_storage.start()
        try:
            yield
        finally:
            try:
                try:
                    try:
                        await app.state.async_storage.close()
                    finally:
                        await app.state.model_gateway.close()
                finally:
                    try:
                        if app.state.async_db is not None:
                            await app.state.async_db.engine.dispose()
                    finally:
                        await app.state.rate_limiter.close()
            finally:
                def close_sync_resources():
                    app.state.http_metrics.close()
                    try:
                        if redis:
                            redis.close()
                    finally:
                        try:
                            app.state.storage.close()
                        finally:
                            database.engine.dispose()
                try:
                    await run_in_threadpool(close_sync_resources)
                finally:
                    limiter.total_tokens = previous_tokens

    app = FastAPI(title="数据集平台", version="0.1.0", lifespan=lifespan,
                  description="统一数据集目录、不可变版本、审批授权与 Agent 接口")
    app.state.settings, app.state.db, app.state.storage = settings, database, make_storage(settings)
    from .metrics import Metrics
    app.state.http_metrics=Metrics(settings)
    # 独立内存 SQLite 引擎对应另一数据库；单元测试保持一个数据库。文件/真实 MySQL 原生异步。
    app.state.async_db = None if ":memory:" in settings.database_url else AsyncDatabase(settings.database_url, settings)
    app.state.model_gateway = None
    from .async_storage import AsyncObjectReader
    app.state.async_storage = AsyncObjectReader(app.state.storage, settings)
    app.state.upload_limiter = CapacityLimiter(settings.upload_max_inflight)
    app.state.download_limiter = CapacityLimiter(settings.download_max_inflight)
    app.state.media_limiter = CapacityLimiter(settings.media_max_inflight)
    app.state.agent_status_limiter=CapacityLimiter(settings.agent_status_max_inflight)
    app.state.agent_status_owners={}
    from .preview_cache import ThumbnailCache
    app.state.preview_cache=ThumbnailCache(settings.preview_cache_bytes,settings.preview_cache_ttl_seconds)
    app.state.mcp_limiter = CapacityLimiter(settings.mcp_max_inflight)
    from .agent import MCPKeyResolver
    app.state.mcp_keys = MCPKeyResolver()
    app.state.oauth = None
    if settings.oidc_issuer:
        oauth = OAuth()
        oauth.register("enterprise", client_id=settings.oidc_client_id, client_secret=settings.oidc_client_secret,
                       server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})
        app.state.oauth = oauth
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, https_only=settings.cookie_secure,
                       same_site="lax", session_cookie="dp_oidc", max_age=600)
    redis = None
    async_redis = None
    if settings.environment == "production":
        from redis import Redis
        from redis.asyncio import Redis as AsyncRedis
        redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        async_redis = AsyncRedis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    app.state.rate_limiter = RateLimiter(settings, database, async_redis)
    request_logger = logging.getLogger("platform.requests")
    request_logger.setLevel(logging.INFO)
    if not request_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        request_logger.addHandler(handler)
    app.add_middleware(RequestGuardMiddleware, settings=settings, limiter=app.state.rate_limiter,metrics=app.state.http_metrics)
    # CORS 位于请求保护外层，使前端也能读取提前拒绝的 413/429/503。
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Range", "MCP-Protocol-Version"],
                       expose_headers=["Content-Range", "Accept-Ranges", "X-Request-ID", "Retry-After"])

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": "too_large" if exc.status_code == 413 else "request_failed", "message": str(exc.detail)},
                             status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = [".".join(str(x) for x in error["loc"]) for error in exc.errors()]
        return JSONResponse({"error": "validation", "message": "请检查输入字段", "fields": fields}, 422)

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"error": "conflict", "message": "记录已存在或存在并发更新，请刷新后重试"}, 409)

    @app.exception_handler(PoolTimeout)
    async def database_busy(request, exc):
        return database_failure(request, exc, "database_busy", "数据库请求繁忙，请稍后重试")

    @app.exception_handler(OperationalError)
    async def database_unavailable(request, exc):
        # 断连、锁等待失败都不自动重放写事务，避免结果不确定时重复创建记录。
        return database_failure(request, exc, "database_unavailable", "数据库操作未完成，请查询状态后重试")

    def database_failure(request, exc, error, message):
        from .diagnostics import report
        report(request_logger,error,exc,request_id=getattr(request.state,'request_id',None),
               method=request.method,path=request.url.path)
        return JSONResponse({"error": error, "message": message}, 503, headers={"Retry-After": "1"})

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        request_id = getattr(request.state, "request_id", uuid4().hex)
        from .diagnostics import report
        report(request_logger,'http_error',exc,request_id=request_id,method=request.method,path=request.url.path)
        return JSONResponse({"error": "internal", "message": "操作未完成，请稍后重试"}, 500,
                            headers={"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff",
                                     "Referrer-Policy": "same-origin", "Cache-Control": "no-store"})

    from . import admin, agent, assistant, auth, datasets, downloads, image_reviews, imports, spaces, tasks, uploads, workflows,review_templates,review_workbench,joint_review
    for module in [auth, spaces, datasets, uploads, workflows, downloads, tasks, admin, image_reviews, imports,review_templates,review_workbench,joint_review]:
        app.include_router(module.router, prefix="/api/v1")
    app.include_router(agent.router)
    app.include_router(assistant.router)
    from . import agent_streams
    app.include_router(agent_streams.router)
    from . import image_completeness
    app.include_router(image_completeness.router)
    from . import governance
    app.include_router(governance.router)
    from . import delivery
    app.include_router(delivery.router)
    from . import ownership
    app.include_router(ownership.router)
    from . import organization_ownership
    app.include_router(organization_ownership.router)
    from . import blind_evaluation
    app.include_router(blind_evaluation.router)
    from . import metrics
    app.include_router(metrics.router)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        result = await run_in_threadpool(ready_sync)
        if not isinstance(result, dict):
            return result
        try:
            if app.state.async_db is not None:
                await app.state.async_db.run(lambda db: db.scalar(text("SELECT 1")))
            return result
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)

    def ready_sync():
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            from pathlib import Path
            expected = ScriptDirectory.from_config(Config(str(Path(__file__).parents[1] / "alembic.ini"))).get_current_head()
            with database.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                installed = connection.scalar(text("SELECT version_num FROM alembic_version"))
                if installed != expected:
                    raise RuntimeError("Schema migration required")
            if settings.storage_backend == "s3":
                app.state.storage.client.head_bucket(Bucket=settings.s3_bucket)
            if redis:
                redis.ping()
                from .operations import presence_key, scan_available
                for role in ("worker", "beat", *(("vision",) if settings.vision_enabled else ()), *(("mail",) if settings.smtp_host else ())):
                    if not next(redis.scan_iter(match=presence_key(settings, role) + "*", count=100), None):
                        raise RuntimeError("Background processing not ready")
                scan_available(settings)
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)

    @app.get("/api/v1/config")
    async def config():
        from .checks import SUPPORTED,MODALITY_FORMATS
        return {"deployment": settings.deployment, "registration_enabled": settings.registration_enabled,
                'registration_email_required': settings.registration_email_required,
                'mail_delivery_mode': settings.mail_delivery_mode,
                'password_reset_available': settings.mail_delivery_mode == 'smtp',
                "verification_required": settings.require_verification, "oidc_enabled": bool(settings.oidc_issuer),
                "model_enabled": bool(settings.model_url and settings.model_name),
                'agent_execution_mode':'tool-calling' if settings.agent_tools_enabled else 'guided',
                "mcp_enabled": bool(settings.mcp_issuer and settings.mcp_audience and settings.mcp_jwks_url),
                "environment": settings.environment, "part_size_bytes": settings.part_size_bytes,
                "max_file_bytes": settings.max_file_bytes, "max_upload_bytes": settings.max_upload_bytes,
                'governance_max_images':settings.governance_max_images,'dedup_max_candidates':settings.dedup_max_candidates,
                'vision_shard_images':settings.vision_max_images,'vision_plan_active_shards':settings.vision_plan_active_shards,
                'agent_content_retention_days':settings.agent_content_retention_days,
                'delivery_max_images':settings.delivery_max_images,'delivery_max_annotations':settings.delivery_max_annotations,
                'acceptance_namespace':settings.acceptance_namespace if settings.environment=='test' else None,
                'vision_attribute_mode':settings.vision_attribute_mode,'vision_image_budget_seconds':settings.vision_image_budget_seconds,
                "supported_formats": sorted(SUPPORTED),
                "modality_formats": {k:sorted(v) for k,v in MODALITY_FORMATS.items()}}

    return app
