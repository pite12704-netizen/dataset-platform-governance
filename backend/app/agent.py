"""【学习标注：外部 Agent 接入】服务凭据管理和 OAuth 保护的只读 MCP Server。

TOOLS/TOOL_CONFIG 声明工具与 scope；mcp_identity 校验签名、issuer、audience、期限和身份；
mcp 解析 JSON-RPC、严格验证参数、调用 assistant_tools 并记录 AgentRun。
"""
import json
import secrets
import time
import threading
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from . import schemas as s
from .agent_tools import TOOL_CONFIG, invoke_tool
from .common import audit, fetch, public_row, require
from .db import get_db, now, run_db
from .models import AgentRun, Identity, Job, ServiceCredential, User
from .security import interactive_user, token_hash

router = APIRouter(tags=["Agent 接入"])


@router.get("/api/v1/credentials")
def credentials(db=Depends(get_db), user=Depends(interactive_user)):
    return [public_row(x) for x in db.scalars(select(ServiceCredential).where(ServiceCredential.user_id == user.id))]


@router.post("/api/v1/credentials", status_code=201)
def create_credential(data: s.CredentialInput, request:Request, db=Depends(get_db), user=Depends(interactive_user)):
    if 'metrics:read' in data.scopes:
        require(user.platform_admin and (not request.app.state.settings.require_admin_mfa or request.state.mfa),'监控凭据只允许已强化认证的管理员创建')
    token = "dp_svc_" + secrets.token_urlsafe(40)
    cred = ServiceCredential(user_id=user.id, name=data.name, scopes=list(set(data.scopes)),
                             token_hash=token_hash(token), expires_at=now() + timedelta(days=data.duration_days))
    db.add(cred)
    db.flush()
    audit(db, user, "credential.create", cred.id, scopes=data.scopes)
    db.commit()
    return {**public_row(cred), "token": token}


@router.delete("/api/v1/credentials/{credential_id}")
def revoke_credential(credential_id: str, db=Depends(get_db), user=Depends(interactive_user)):
    cred = fetch(db, ServiceCredential, credential_id)
    require(cred.user_id == user.id, "凭据不存在", 404)
    cred.revoked = True
    audit(db, user, "credential.revoke", cred.id)
    db.commit()
    return {"message": "凭据已撤销"}


TOOLS = [{"name": name, "description": description, "inputSchema": schema.model_json_schema(),
          "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}}
         for name, (schema, scope, description) in TOOL_CONFIG.items()]


@router.get("/.well-known/oauth-protected-resource")
def resource_metadata(request: Request):
    settings = request.app.state.settings
    require(settings.mcp_issuer and settings.mcp_audience, "MCP OAuth 服务尚未配置", 503)
    return {"resource": settings.mcp_audience, "authorization_servers": [settings.mcp_issuer],
            "scopes_supported": ["catalog:read", "content:read", "jobs:read"], "bearer_methods_supported": ["header"]}


class MCPKeyResolver:
    """每个应用进程复用 JWKS；串行刷新避免并发请求同时抓取密钥。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.configuration = None
        self.client = None
        self.unavailable_until = 0

    def signing_key(self, token, settings):
        configuration = (settings.mcp_jwks_url, settings.mcp_jwks_timeout_seconds,
                         settings.mcp_jwks_cache_seconds)
        acquired = self.lock.acquire(timeout=settings.mcp_jwks_timeout_seconds)
        if not acquired:
            raise jwt.PyJWKClientConnectionError("JWKS refresh busy")
        try:
            if self.configuration != configuration:
                self.client = jwt.PyJWKClient(configuration[0], timeout=configuration[1],
                                            lifespan=configuration[2], cache_keys=False)
                self.configuration = configuration
                self.unavailable_until = 0
            if time.monotonic() < self.unavailable_until:
                raise jwt.PyJWKClientConnectionError("JWKS temporarily unavailable")
            try:
                return self.client.get_signing_key_from_jwt(token)
            except jwt.PyJWKClientConnectionError:
                self.unavailable_until = time.monotonic() + 5
                raise
        finally:
            self.lock.release()


def mcp_identity(request, db):
    settings = request.app.state.settings
    require(settings.mcp_issuer and settings.mcp_audience and settings.mcp_jwks_url, "MCP OAuth 服务尚未配置", 503)
    auth = request.headers.get("authorization", "")
    from fastapi import HTTPException
    challenge = {"WWW-Authenticate": 'Bearer resource_metadata="' + settings.public_url + '/.well-known/oauth-protected-resource"'}
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "需要 OAuth 访问令牌", headers=challenge)
    try:
        token = auth[7:]
        key = request.app.state.mcp_keys.signing_key(token, settings)
        claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=settings.mcp_audience,
                            issuer=settings.mcp_issuer, options={"require": ["exp", "sub", "iss", "aud"]})
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "企业身份密钥服务暂不可用，请稍后重试", headers={"Retry-After": "5"})
    except (jwt.PyJWTError, OSError):
        raise HTTPException(401, "OAuth 访问令牌无效", headers=challenge)
    identity = db.scalar(select(Identity).where(Identity.issuer == settings.mcp_issuer,
                                               Identity.subject == claims["sub"]))
    user = db.get(User, identity.user_id) if identity else None
    require(user and user.active and user.verified, "身份未关联或已停用")
    require(isinstance(claims.get("scope", ""), str), "OAuth scope 必须为空格分隔的字符串", 401)
    return user, set(claims.get("scope", "").split())


@router.post("/mcp")
async def mcp(request: Request):
    # 接收请求体时不占用数据库会话；认证、工具、审计全部在线程内完成。
    parse_error = False
    try:
        body = await request.json()
    except ValueError:
        body, parse_error = None, True
    from .concurrency import admission
    async with admission(request.app.state.mcp_limiter, 'MCP 并发已满，请稍后重试'):
        return await run_db(request, dispatch_mcp, request, body, parse_error)


def dispatch_mcp(db, request, body, parse_error=False):
    user, scopes = mcp_identity(request, db)
    origin = request.headers.get("origin")
    require(not origin or origin in request.app.state.settings.cors_origins, "MCP 请求来源不被允许")
    if parse_error:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    require(isinstance(body, dict) and body.get("jsonrpc") == "2.0", "无效 JSON-RPC 请求", 400)
    method, call_id = body.get("method"), body.get("id")
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "dataset-platform", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [tool for tool in TOOLS if TOOL_CONFIG[tool["name"]][1] in scopes]}
    elif method == "tools/call":
        params = body.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return {"jsonrpc": "2.0", "id": call_id, "error": {"code": -32602, "message": "Invalid tool parameters"}}
        name, arguments = params.get("name"), params.get("arguments", {})
        if name not in TOOL_CONFIG:
            return {"jsonrpc": "2.0", "id": call_id, "error": {"code": -32602, "message": "Unknown tool"}}
        schema, required, _ = TOOL_CONFIG[name]
        try:
            args = schema.model_validate(arguments)
        except ValidationError:
            return {"jsonrpc": "2.0", "id": call_id, "error": {"code": -32602, "message": "Invalid tool arguments"}}
        started = time.monotonic()
        settings = request.app.state.settings
        failed = False
        try:
            require(required in scopes, "OAuth 令牌缺少工具所需权限")
            payload = invoke_tool(name, args, db, user, settings, scopes, request.app.state.storage)
        except HTTPException as exc:
            failed = True
            payload = {"error": str(exc.detail)}
        except (OSError, ValueError):
            failed = True
            payload = {"error": "工具依赖暂不可用，请稍后重试"}
        db.add(AgentRun(user_id=user.id, source="mcp", tool=name, model="mcp-tools", inputs=args.model_dump(),
                       outputs={"status": "failed" if failed else "returned"}, status="failed" if failed else "completed",
                       duration_ms=int((time.monotonic() - started) * 1000)))
        audit(db, user, "agent.tool", tool=name, status="failed" if failed else "completed")
        db.commit()
        result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
                  "isError": failed}
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": call_id, "error": {"code": -32601, "message": "Method not found"}})
    return {"jsonrpc": "2.0", "id": call_id, "result": result}
