"""【学习标注：认证安全】处理密码、JWT 会话、服务凭据、MFA、CSRF 和 scope 校验。

重点区分 current_user（允许服务身份）和 interactive_user（必须是真人交互登录）。
普通业务写入依赖 interactive_user；批量导入仅在 imports 模块的批次范围依赖中另行验证。
"""
import base64
import hashlib
import hmac
import secrets
import time
from datetime import timedelta

import jwt
import pyotp
from cryptography.fernet import Fernet, MultiFernet
from fastapi import Depends, Request
from sqlalchemy import select

from .common import require
from .db import get_db, now
from .models import LoginSession, ServiceCredential, User


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password, encoded):
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def cipher(settings):
    keys=[Fernet(key.encode()) for key in settings.mfa_encryption_keys]
    if settings.mfa_allow_legacy_key:
        keys.append(Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())))
    return MultiFernet(keys)


def verify_mfa(db, user, code, settings):
    require(user.mfa_enabled and bool(code), "需要动态验证码", 401)
    totp = pyotp.TOTP(cipher(settings).decrypt(user.mfa_secret.encode()).decode())
    counter = int(time.time()) // 30
    valid_counter = next((c for c in [counter - 1, counter, counter + 1]
                          if hmac.compare_digest(totp.generate_otp(c), code)), None)
    require(valid_counter is not None and valid_counter > user.mfa_last_counter,
            "动态验证码无效或已使用", 401)
    user.mfa_last_counter = valid_counter


def issue_session(db, user, settings, response, mfa=False, recovery_only=False):
    session = LoginSession(user_id=user.id, expires_at=now() + (timedelta(minutes=10) if recovery_only else timedelta(hours=settings.session_hours)), mfa=mfa,recovery_only=recovery_only)
    db.add(session)
    db.flush()
    token = jwt.encode({"sub": user.id, "jti": session.id, "aud": "dataset-platform",
                        "iss": "dataset-platform", "exp": session.expires_at,
                        "iat": now()}, settings.jwt_signing_key, algorithm="HS256")
    response.set_cookie("dp_session", token, httponly=True, secure=settings.cookie_secure,
                        samesite="lax", max_age=600 if recovery_only else settings.session_hours * 3600, path="/")
    return token


def optional_user(request: Request, db=Depends(get_db)):
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else request.cookies.get("dp_session")
    request.state.scopes = None
    request.state.service_identity = False
    request.state.mfa = False
    request.state.recovery_only = False
    if not token:
        return None
    settings = request.app.state.settings
    if token.startswith("dp_svc_"):
        cred = db.scalar(select(ServiceCredential).where(ServiceCredential.token_hash == token_hash(token)))
        require(cred and not cred.revoked and cred.expires_at > now(), "服务凭据无效", 401)
        require(request.method in {"GET", "HEAD", "OPTIONS"} or request.url.path.endswith("/agent/chat"),
                "服务凭据仅允许读取数据", 403)
        user = db.get(User, cred.user_id)
        request.state.scopes = set(cred.scopes)
        request.state.service_identity = True
    else:
        try:
            claims = jwt.decode(token, settings.jwt_signing_key, algorithms=["HS256"], audience="dataset-platform",
                                issuer="dataset-platform", options={"require": ["exp", "sub", "jti"]})
        except jwt.PyJWTError:
            require(False, "登录已失效", 401)
        session = db.get(LoginSession, claims["jti"])
        require(session and not session.revoked and session.expires_at > now()
                and session.user_id == claims["sub"], "登录已失效", 401)
        user = db.get(User, session.user_id)
        request.state.session_id = session.id
        request.state.mfa = session.mfa
        request.state.recovery_only = session.recovery_only
    require(user and user.active, "账号已停用", 401)
    if request.state.recovery_only:
        require(request.url.path in {'/api/v1/auth/me','/api/v1/auth/logout','/api/v1/auth/mfa/recover'},
                '恢复码登录仅用于重置丢失的 MFA，请先完成恢复',403)
    if user.must_change_password:
        require(request.url.path in {'/api/v1/auth/me', '/api/v1/auth/logout', '/api/v1/auth/change-password',
                                    '/api/v1/auth/mfa/setup', '/api/v1/auth/mfa/confirm','/api/v1/auth/mfa/recover'},
                '请先修改初始或重置密码，再使用平台业务功能', 403)
    if not request.state.service_identity and request.method not in {"GET", "HEAD", "OPTIONS"} and not auth:
        require(request.headers.get("x-requested-with") == "dataset-platform", "缺少请求来源校验", 403)
        origin = request.headers.get("origin")
        require(not origin or origin in settings.cors_origins or origin == settings.public_url,
                "请求来源不被允许", 403)
    return user


def current_user(user=Depends(optional_user)):
    require(user is not None, "请先登录", 401)
    require(user.verified, "请先验证账号", 403)
    return user


def interactive_user(request: Request, user=Depends(current_user)):
    require(not request.state.service_identity, "此功能需要用户交互登录")
    return user


def resolve_interactive_user(request: Request, db):
    """在同一线程与会话内重新认证，供异步接口的每个数据库阶段使用。"""
    return interactive_user(request, current_user(optional_user(request, db)))


def admin_user(request: Request, user=Depends(interactive_user)):
    require(user.platform_admin, "需要平台管理员权限")
    require(not request.app.state.settings.require_admin_mfa or request.state.mfa,
            "管理员操作需要绑定 MFA 后重新登录")
    return user


def enforce_scope(request, scope):
    scopes = getattr(request.state, "scopes", None)
    require(scopes is None or scope in scopes, f"凭据缺少 {scope} 权限")
