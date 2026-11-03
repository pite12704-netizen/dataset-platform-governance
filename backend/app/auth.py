"""【学习标注：账号与登录】注册、验证、登录、退出、找回密码、MFA 与企业 OIDC。

主链路：register/add_user → challenge/deliver_mail → login/issue_session。
"""
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage

import pyotp
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from . import schemas as s
from .common import audit, public_row, require,fetch
from .db import get_db, now, run_db
from .models import Challenge, Identity, ImportBatch, LoginSession, MailOutbox, Membership, ServiceCredential, Space, User
from .security import (cipher, hash_password, interactive_user, issue_session, optional_user,
                       token_hash, verify_mfa, verify_password)

router = APIRouter(prefix="/auth", tags=["身份认证"])


def add_user(db, data, settings, verified=False, admin=False):
    require(not data.email or not db.scalar(select(User.id).where(User.email == data.email)), "账号已存在", 409)
    require(not data.username or not db.scalar(select(User.id).where(User.username == data.username)), '账号已存在', 409)
    user = User(email=data.email, username=data.username, name=data.name, password_hash=hash_password(data.password),
                verified=verified, platform_admin=admin)
    db.add(user)
    db.flush()
    from .capacity import account_provision
    account_provision(db, user.id, settings.default_quota_bytes, settings)
    space = Space(name=f"{user.name}的空间", kind="personal", owner_id=user.id,
                  quota_bytes=settings.default_quota_bytes)
    db.add(space)
    db.flush()
    db.add(Membership(space_id=space.id, user_id=user.id, role="admin"))
    return user


def challenge(db, user, kind, settings):
    require(user.email, '此账号未绑定邮箱，请联系管理员', 409)
    token = secrets.token_urlsafe(32)
    db.add(Challenge(user_id=user.id, kind=kind, token_hash=token_hash(token),
                     expires_at=now() + timedelta(hours=1)))
    link = f"{settings.frontend_url}/?{kind}={token}"
    db.add(MailOutbox(recipient=user.email, subject="数据集平台：账号验证" if kind == "verify" else "数据集平台：重置密码",
                      body=f"请在一小时内打开以下链接：\n{link}\n如果并非本人操作，请忽略此邮件。"))


def deliver_mail(db, settings):
    from .mail_delivery import deliver
    return deliver(db,settings,smtplib,ssl,EmailMessage)


@router.post("/register", status_code=201)
def register(data: s.Register, request: Request, db=Depends(get_db)):
    settings = request.app.state.settings
    require(settings.registration_enabled, "当前关闭自行注册，请联系管理员创建账号")
    require(not settings.registration_email_required or data.email, '当前注册策略要求邮箱', 422)
    require(not settings.require_verification or settings.mail_delivery_mode != 'disabled', '邮件验证服务未配置，暂不能注册', 503)
    user = add_user(db, data, settings, verified=not settings.require_verification)
    if settings.require_verification:
        challenge(db, user, "verify", settings)
    audit(db, user, "user.register", user.id)
    db.commit()
    if settings.task_mode == "inline":
        deliver_mail(db, settings)
    return {"message": "账号已创建，请查收验证邮件" if settings.require_verification else "账号已创建"}


@router.post("/login")
def login(data: s.Login, request: Request, response: Response, db=Depends(get_db)):
    account = (data.account or data.email).lower()
    column = User.email if '@' in account else User.username
    user = db.scalar(select(User).where(column == account).with_for_update())
    require(user and verify_password(data.password, user.password_hash), "账号或密码错误", 401)
    require(user.active and user.verified, "账号未验证或已停用", 403)
    if user.mfa_enabled:
        if data.recovery_code:
            digest=token_hash(user.id+':'+data.recovery_code.lower().replace('-',''))
            require(digest in user.mfa_recovery_hashes,'恢复码无效或已使用',401)
            user.mfa_recovery_hashes=[value for value in user.mfa_recovery_hashes if value!=digest]
        else:verify_mfa(db, user, data.code, request.app.state.settings)
    recovery=bool(user.mfa_enabled and data.recovery_code)
    token = issue_session(db, user, request.app.state.settings, response, mfa=user.mfa_enabled and not recovery,recovery_only=recovery)
    audit(db, user, "user.login", user.id)
    db.commit()
    return {"user": public_row(user), "access_token": token, "token_type": "bearer",
            'mfa_recovery_required':recovery,
            'password_change_required': user.must_change_password,
            "mfa_setup_required": user.platform_admin and request.app.state.settings.require_admin_mfa
            and not user.mfa_enabled}


@router.get("/me")
def me(request:Request,user=Depends(interactive_user)):
    return {**public_row(user),'recovery_only':request.state.recovery_only}


@router.post("/logout")
def logout(request: Request, response: Response, db=Depends(get_db), user=Depends(interactive_user)):
    session = db.get(LoginSession, request.state.session_id)
    session.revoked = True
    db.commit()
    response.delete_cookie("dp_session", path="/")
    return {"message": "已退出"}


@router.post("/verify")
def verify(data: s.TokenInput, db=Depends(get_db)):
    item = consume_challenge(db, data.token, "verify")
    db.get(User, item.user_id).verified = True
    db.commit()
    return {"message": "账号验证成功"}


def consume_challenge(db, token, kind):
    user_id = db.scalar(select(Challenge.user_id).where(Challenge.token_hash == token_hash(token)))
    require(user_id is not None, '链接无效或已过期', 400)
    db.scalar(select(User).where(User.id == user_id).with_for_update())
    item = db.scalar(select(Challenge).where(Challenge.token_hash == token_hash(token))
                     .execution_options(populate_existing=True).with_for_update())
    require(item and item.kind == kind and not item.consumed and item.expires_at > now(), "链接无效或已过期", 400)
    item.consumed = True
    return item


@router.post("/forgot-password")
def forgot(data: s.EmailInput, request: Request, db=Depends(get_db)):
    require(request.app.state.settings.mail_delivery_mode != 'disabled', '邮件找回尚未开通，请联系平台管理员重置密码', 503)
    user = db.scalar(select(User).where(User.email == data.email.lower(), User.active.is_(True)).with_for_update())
    if user:
        recent = db.scalar(select(Challenge.id).where(Challenge.user_id == user.id, Challenge.kind == 'reset',
            Challenge.created_at > now() - timedelta(minutes=1)).limit(1))
        if not recent:
            challenge(db, user, "reset", request.app.state.settings)
        db.commit()
        if request.app.state.settings.task_mode == "inline":
            deliver_mail(db, request.app.state.settings)
    return {'message': '请求已受理；若账号存在，将尝试发送重置链接。此提示不代表邮件已送达。',
            'delivery_mode': request.app.state.settings.mail_delivery_mode}


@router.post('/resend-verification')
def resend_verification(data:s.EmailInput,request:Request,db=Depends(get_db)):
    cfg=request.app.state.settings
    require(cfg.mail_delivery_mode!='disabled','邮件服务尚未开通',503)
    user=db.scalar(select(User).where(User.email==data.email.lower(),User.active.is_(True),User.verified.is_(False)).with_for_update())
    if user:
        recent=db.scalar(select(Challenge.id).where(Challenge.user_id==user.id,Challenge.kind=='verify',Challenge.created_at>now()-timedelta(minutes=1)).limit(1))
        if not recent:
            db.execute(update(Challenge).where(Challenge.user_id==user.id,Challenge.kind=='verify').values(consumed=True))
            challenge(db,user,'verify',cfg);db.commit()
            if cfg.task_mode=='inline':deliver_mail(db,cfg)
    return {'message':'请求已受理；符合条件的账号会收到验证邮件，提示不代表实际送达'}


def revoke_account_credentials(db,user):
    db.execute(update(LoginSession).where(LoginSession.user_id == user.id).values(revoked=True))
    db.execute(update(ServiceCredential).where(ServiceCredential.user_id == user.id).values(revoked=True))
    db.execute(update(ImportBatch).where(ImportBatch.user_id == user.id).values(revoked=True))
    db.execute(update(Challenge).where(Challenge.user_id == user.id, Challenge.kind.in_({'reset','email_change'})).values(consumed=True))


def replace_password(db, user, password, force_change=False):
    user.password_hash = hash_password(password)
    user.must_change_password = force_change
    revoke_account_credentials(db,user)


@router.post("/reset-password")
def reset(data: s.Reset, db=Depends(get_db)):
    item = consume_challenge(db, data.token, "reset")
    user = db.get(User, item.user_id)
    require(user.active, '账号已停用', 403)
    replace_password(db, user, data.password)
    audit(db, user, "user.password_reset", user.id)
    db.commit()
    return {"message": "密码已更新，请重新登录"}


@router.post('/change-password')
def change_password(data: s.ChangePassword, request:Request, db=Depends(get_db), user=Depends(interactive_user)):
    user = locked_actor(db,user,request)
    require(verify_password(data.current_password, user.password_hash), '当前密码不正确', 400)
    require(not verify_password(data.password, user.password_hash), '新密码不能与当前密码相同', 422)
    replace_password(db, user, data.password)
    audit(db, user, 'user.password_change', user.id); db.commit()
    return {'message': '密码已更新，旧会话与服务/导入凭据已失效，请重新登录'}


@router.put('/username')
def set_username(data: s.UsernameInput, request:Request, db=Depends(get_db), user=Depends(interactive_user)):
    user=locked_actor(db,user,request)
    require(verify_password(data.current_password, user.password_hash), '当前密码不正确', 400)
    username = data.username.lower()
    require(not db.scalar(select(User.id).where(User.username == username, User.id != user.id)), '账号已被使用', 409)
    user.username = username
    audit(db, user, 'user.username_change', user.id); db.commit()
    return public_row(user)


@router.post("/mfa/setup")
def mfa_setup(request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    user=locked_actor(db,user,request)
    require(not user.mfa_enabled, "MFA 已启用", 409)
    secret = pyotp.random_base32()
    user.mfa_secret = cipher(request.app.state.settings).encrypt(secret.encode()).decode()
    db.commit()
    return {"secret": secret, "uri": pyotp.TOTP(secret).provisioning_uri(user.email or user.username or user.id, issuer_name="Dataset Platform")}


@router.post("/mfa/confirm")
def mfa_confirm(data: s.CodeInput, request: Request, db=Depends(get_db), user=Depends(interactive_user)):
    user=locked_actor(db,user,request)
    require(user.mfa_secret and not user.mfa_enabled, "请先创建 MFA 配置", 409)
    user.mfa_enabled = True
    verify_mfa(db, user, data.code, request.app.state.settings)
    db.get(LoginSession, request.state.session_id).mfa = True
    audit(db, user, "user.mfa_enabled", user.id)
    codes=make_recovery_codes(user);db.commit()
    return {"message": "MFA 已启用，恢复码仅展示一次，请离线保存",'recovery_codes':codes}


def make_recovery_codes(user):
    raw=[secrets.token_hex(12) for _ in range(8)]
    user.mfa_recovery_hashes=[token_hash(user.id+':'+value) for value in raw]
    return ['-'.join(value[i:i+8] for i in range(0,24,8)) for value in raw]


@router.post('/mfa/recovery-codes')
def recovery_codes(data:s.SensitiveAccount,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    user=locked_actor(db,user,request)
    require(user.mfa_enabled and verify_password(data.current_password,user.password_hash),'密码或 MFA 状态不正确',401)
    verify_mfa(db,user,data.code,request.app.state.settings)
    codes=make_recovery_codes(user)
    audit(db,user,'user.mfa_recovery.rotate',user.id);db.commit()
    return {'recovery_codes':codes,'message':'旧恢复码已失效，新恢复码仅展示一次'}


@router.post('/mfa/recover')
def recover_mfa(data:s.SensitiveAccount,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    user=locked_actor(db,user,request)
    require(request.state.recovery_only,'请使用恢复码及账号密码重新登录',403)
    require(verify_password(data.current_password,user.password_hash),'当前密码不正确',401)
    user.mfa_enabled=False;user.mfa_secret=None;user.mfa_last_counter=-1;user.mfa_recovery_hashes=[]
    revoke_account_credentials(db,user)
    audit(db,user,'user.mfa_recovered',user.id);db.commit()
    return {'message':'旧 MFA 和所有凭据已撤销，请重新登录并绑定认证器'}


def locked_actor(db,user,request):
    user=fetch(db,User,user.id,lock=True)
    session=fetch(db,LoginSession,request.state.session_id,lock=True)
    require(user.active and user.verified and not session.revoked and session.expires_at>now() and session.user_id==user.id,
            '账号或会话已失效',401)
    request.state.mfa=session.mfa;request.state.recovery_only=session.recovery_only
    return user


def sensitive_user(db,user,data,settings,request):
    user=locked_actor(db,user,request)
    require(verify_password(data.current_password,user.password_hash),'当前密码不正确',401)
    if user.mfa_enabled:verify_mfa(db,user,data.code,settings)
    return user


@router.post('/email/change')
def email_change(data:s.EmailChange,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    cfg=request.app.state.settings
    require(cfg.mail_delivery_mode!='disabled','邮件服务尚未开通，不能绑定或更换邮箱',503)
    user=sensitive_user(db,user,data,cfg,request);email=data.email.lower()
    require(email!=user.email,'新邮箱与当前邮箱相同',409)
    require(not db.scalar(select(User.id).where(User.email==email)),'邮箱不可用',409)
    recent=db.scalar(select(Challenge.id).where(Challenge.user_id==user.id,Challenge.kind=='email_change',
        Challenge.created_at>now()-timedelta(minutes=1)).limit(1))
    require(not recent,'请求过于频繁，请稍后重试',429)
    db.execute(update(Challenge).where(Challenge.user_id==user.id,Challenge.kind=='email_change').values(consumed=True))
    token=secrets.token_urlsafe(32)
    db.add(Challenge(user_id=user.id,kind='email_change',token_hash=token_hash(token),
        expires_at=now()+timedelta(minutes=30),payload={'email':email,'previous_email':user.email}))
    db.add(MailOutbox(recipient=email,subject='数据集平台：确认绑定邮箱',
        body=f'请在30分钟内以原账号登录并确认新邮箱：\n{cfg.frontend_url}/?bind_email={token}'))
    if user.email:db.add(MailOutbox(recipient=user.email,subject='数据集平台：邮箱变更提醒',body='你的账号申请了更换邮箱。如非本人操作，请立即修改密码并联系管理员。'))
    audit(db,user,'user.email_change.request',user.id);db.commit()
    if cfg.task_mode=='inline':deliver_mail(db,cfg)
    return {'message':'验证请求已登记，请检查新邮箱；登记不等于实际送达','delivery_mode':cfg.mail_delivery_mode}


@router.post('/email/confirm')
def email_confirm(data:s.EmailConfirm,request:Request,db=Depends(get_db),user=Depends(interactive_user)):
    require(db.scalar(select(Challenge.id).where(Challenge.token_hash==token_hash(data.token),Challenge.user_id==user.id)),
            '链接不属于当前账号',403)
    user=sensitive_user(db,user,data,request.app.state.settings,request)
    item=consume_challenge(db,data.token,'email_change')
    require(item.user_id==user.id and item.payload.get('previous_email')==user.email,'链接不属于当前账号或账号已变更',403)
    email=item.payload['email']
    require(not db.scalar(select(User.id).where(User.email==email,User.id!=user.id)),'邮箱不可用',409)
    user.email=email;user.verified=True
    revoke_account_credentials(db,user)
    audit(db,user,'user.email_change.confirm',user.id);db.commit()
    return {'message':'邮箱已验证并绑定，旧会话与凭据已撤销，请重新登录'}


@router.post('/mail/confirm-probe')
def confirm_mail_probe(data:s.TokenInput,db=Depends(get_db),user=Depends(interactive_user)):
    require(user.platform_admin,'需要发起测试的管理员登录')
    require(db.scalar(select(Challenge.id).where(Challenge.user_id==user.id,Challenge.token_hash==token_hash(data.token))),
        '此收件确认不属于当前账号',403)
    item=consume_challenge(db,data.token,'mail_probe')
    mail=fetch(db,MailOutbox,item.payload['outbox_id'])
    require(mail.delivery_mode=='smtp' and mail.sent_at,'文件测试投递不能认证真实 SMTP 收件',409)
    item.payload={**item.payload,'received_at':now().isoformat()+'Z'}
    audit(db,user,'mail.probe.received',item.id);db.commit()
    return {'message':'真实收件已确认，仅证明本次测试地址收到此邮件'}


@router.get("/oidc/login")
async def oidc_login(request: Request):
    oauth = request.app.state.oauth
    require(oauth is not None, "企业身份服务未配置", 503)
    return await oauth.enterprise.authorize_redirect(request, request.app.state.settings.public_url + "/api/v1/auth/oidc/callback")


@router.get("/oidc/callback")
async def oidc_callback(request: Request):
    settings = request.app.state.settings
    require(request.app.state.oauth is not None, "企业身份服务未配置", 503)
    token = await request.app.state.oauth.enterprise.authorize_access_token(request)
    info = token.get("userinfo", {})
    return await run_db(request, finish_oidc_login, request, info)


def finish_oidc_login(db, request, info):
    """异步 OAuth 请求结束后，在线程内完成身份关联、密码散列和登录事务。"""
    settings = request.app.state.settings
    require(info.get("sub") and info.get("iss") == settings.oidc_issuer, "身份签发方不匹配", 401)
    identity = db.scalar(select(Identity).where(Identity.issuer == settings.oidc_issuer,
                                               Identity.subject == info["sub"]))
    if identity:
        user = db.get(User, identity.user_id)
    else:
        require(settings.deployment == "public" and info.get("email_verified") is True,
                "请由管理员预先关联企业身份")
        # Do not silently merge an external identity into an existing local account.
        data = s.Register(email=info["email"], name=info.get("name", info["email"]), password=secrets.token_urlsafe(32))
        user = add_user(db, data, settings, verified=True)
        db.add(Identity(issuer=settings.oidc_issuer, subject=info["sub"], user_id=user.id))
    require(user.active and user.verified, "账号已停用", 403)
    response = RedirectResponse(settings.frontend_url)
    # Enterprise login does not bypass locally required privileged-action MFA.
    issue_session(db, user, settings, response, mfa=False)
    audit(db, user, "user.oidc_login", user.id)
    db.commit()
    return response
