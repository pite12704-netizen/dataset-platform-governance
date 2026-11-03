"""Claim, transmit outside a SQL transaction, then fence the per-message receipt."""
from datetime import timedelta
from sqlalchemy import and_, or_, select, update
from .db import now, uid
from .models import MailOutbox


def available():
    return and_(MailOutbox.sent_at.is_(None),MailOutbox.attempts<5,
        or_(MailOutbox.next_attempt_at.is_(None),MailOutbox.next_attempt_at<=now()),
        or_(MailOutbox.lease_until.is_(None),MailOutbox.lease_until<=now()))


def deliver(db,settings,transports,tls,Message):
    if settings.mail_delivery_mode=='disabled':return {'sent':0,'failed':0}
    identifiers=db.scalars(select(MailOutbox.id).where(available()).order_by(MailOutbox.created_at,MailOutbox.id).limit(20)).all()
    db.commit()
    totals={'sent':0,'failed':0}
    for identifier in identifiers:
        item=db.scalar(select(MailOutbox).where(MailOutbox.id==identifier,available())
            .execution_options(populate_existing=True).with_for_update(skip_locked=True))
        if not item:
            db.rollback();continue
        token=uid();item.delivery_token=token;item.lease_until=now()+timedelta(minutes=2)
        item.next_attempt_at=now()+timedelta(seconds=min(900,30*2**item.attempts));item.attempts+=1
        recipient,subject,body=item.recipient,item.subject,item.body
        db.commit()
        error=None
        try:
            message=Message();message['From']=settings.mail_from;message['To']=recipient;message['Subject']=subject
            message['Message-ID']=f'<dp-{identifier}@{settings.mail_from.rsplit("@",1)[-1]}>'
            message.set_content(body)
            if settings.smtp_host:
                context=tls.create_default_context(cafile=str(settings.smtp_ca_file) if settings.smtp_ca_file else None)
                transport=transports.SMTP_SSL if settings.smtp_ssl else transports.SMTP
                with transport(settings.smtp_host,settings.smtp_port,timeout=10,**({'context':context} if settings.smtp_ssl else {})) as smtp:
                    if settings.smtp_starttls and not settings.smtp_ssl:smtp.starttls(context=context)
                    if settings.smtp_username:smtp.login(settings.smtp_username,settings.smtp_password)
                    smtp.send_message(message)
                mode='smtp'
            else:
                settings.mail_spool.mkdir(parents=True,exist_ok=True)
                (settings.mail_spool/f'{identifier}.eml').write_bytes(message.as_bytes());mode='spool'
        except (OSError,transports.SMTPException) as exc:error=type(exc).__name__
        values={'delivery_token':None,'lease_until':None,'last_error':error}
        if error is None:
            values.update(sent_at=now(),delivery_mode=mode,next_attempt_at=None,
                          body='[已发送，验证链接已从发件队列清除]')
        changed=db.execute(update(MailOutbox).where(MailOutbox.id==identifier,MailOutbox.delivery_token==token,
            MailOutbox.sent_at.is_(None)).values(**values))
        db.commit()
        if changed.rowcount:totals['failed' if error else 'sent']+=1
    return totals
