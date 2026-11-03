"""Bounded labels and histograms. Production counters are shared through Redis."""
import threading
import uuid
from collections import Counter
from fastapi import APIRouter,Depends,Request
from fastapi.responses import Response
from sqlalchemy import select,func,or_,and_
from datetime import timedelta
from .common import require
from .db import get_db,now
from .models import Job,MailOutbox,Space
from .security import current_user,enforce_scope,admin_user

BUCKETS=(.05,.1,.25,.5,1,2,5,15,60,180)
GROUPS={'auth','uploads','assets','jobs','agent','datasets','versions','governance','admin','deliveries','evaluation-sets'}


class Metrics:
    def __init__(self,settings):
        self.settings=settings;self.pending=Counter();self.total=Counter();self.lock=threading.Lock()
        self.stop_event=threading.Event();self.thread=None;self.redis=None;self.key=None;self.batch=None
        if settings.environment=='production':
            from redis import Redis
            from .operations import presence_key
            self.redis=Redis.from_url(settings.redis_url,socket_connect_timeout=2,socket_timeout=2,decode_responses=True)
            self.key=presence_key(settings,'http-metrics').rstrip(':')

    def observe(self,path,method,status,seconds):
        parts=path.strip('/').split('/');group=parts[2] if len(parts)>2 and parts[:2]==['api','v1'] else 'other'
        if group not in GROUPS:group='other'
        method=method if method in {'GET','POST','PUT','DELETE','PATCH','OPTIONS','HEAD'} else 'OTHER'
        prefix=f'{group}|{method}|{status//100}xx|'
        values={prefix+'count':1,prefix+'sum_us':round(seconds*1000000),prefix+'le_inf':1}
        for bucket in BUCKETS:
            values[prefix+'le_'+str(bucket)]=int(seconds<=bucket)
        with self.lock:self.pending.update(values);self.total.update(values)

    def flush(self):
        if not self.redis:return
        with self.lock:
            if self.batch is None and self.pending:
                self.batch=(uuid.uuid4().hex,self.pending);self.pending=Counter()
            batch=self.batch
        if not batch:return
        identifier,pending=batch
        try:
            script="if redis.call('EXISTS',KEYS[2])==1 then return 0 end; for i=1,#ARGV,2 do redis.call('HINCRBY',KEYS[1],ARGV[i],ARGV[i+1]) end; redis.call('SET',KEYS[2],'1','EX',604800); return 1"
            arguments=[value for key,count in pending.items() for value in (key,count)]
            self.redis.eval(script,2,self.key,self.key+':batch:'+identifier,*arguments)
            with self.lock:
                if self.batch and self.batch[0]==identifier:self.batch=None
        except Exception:pass  # Bounded pending counters and the same batch ID are retried.

    def start(self):
        if not self.redis:return
        def loop():
            while not self.stop_event.wait(2):self.flush()
        self.thread=threading.Thread(target=loop,daemon=True,name='metrics-flush');self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(3)
        self.flush()
        if self.redis:self.redis.close()

    def snapshot(self):
        if self.redis:
            self.flush()
            return {key:int(value) for key,value in self.redis.hgetall(self.key).items()}
        with self.lock:return dict(self.total)


router=APIRouter(tags=['运行指标'])


@router.get('/api/v1/admin/metrics')
def metrics(request:Request,db=Depends(get_db),user=Depends(current_user)):
    require(user.platform_admin,'需要平台监控权限')
    enforce_scope(request,'metrics:read')
    require(request.state.service_identity or not request.app.state.settings.require_admin_mfa or request.state.mfa,'监控操作需要 MFA')
    try:values=request.app.state.http_metrics.snapshot()
    except Exception:require(False,'共享指标存储不可用，不能用零值替代',503)
    lines=['# TYPE platform_http_requests_total counter','# TYPE platform_http_duration_seconds histogram']
    for key,value in sorted(values.items()):
        group,method,status,metric=key.split('|')
        labels=f'group="{group}",method="{method}",status="{status}"'
        if metric=='count':lines.append(f'platform_http_requests_total{{{labels}}} {value}')
        elif metric=='sum_us':lines.append(f'platform_http_duration_seconds_sum{{{labels}}} {value/1000000}')
        else:
            bucket='+Inf' if metric=='le_inf' else metric.removeprefix('le_')
            lines.append(f'platform_http_duration_seconds_bucket{{{labels},le="{bucket}"}} {value}')
        if metric=='count':lines.append(f'platform_http_duration_seconds_count{{{labels}}} {value}')
    pending=db.execute(select(Job.kind,Job.status,func.count(),func.min(Job.created_at)).where(or_(Job.status.in_({'queued','running'}),
        and_(Job.status=='failed',Job.finished_at>now()-timedelta(hours=1))))
        .group_by(Job.kind,Job.status)).all()
    for kind,status,count,oldest in pending:
        kind=kind if kind in {'scan','thumbnail','quality','convert','package','release','dedup','split','curate','evaluate','vision','vision_plan'} else 'other'
        lines.append(f'platform_jobs{{kind="{kind}",status="{status}"}} {count}')
        if status=='queued':lines.append(f'platform_queue_oldest_seconds{{kind="{kind}"}} {max(0,(now()-oldest).total_seconds())}')
    mail=db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.sent_at.is_(None)))
    lines.append(f'platform_mail_pending {mail}')
    collector=request.app.state.http_metrics
    if collector.redis:
        from .operations import presence_key
        for role in ('worker','vision','beat',*(('mail',) if collector.settings.smtp_host else ())):
            alive=next(collector.redis.scan_iter(match=presence_key(collector.settings,role)+'*',count=20),None) is not None
            lines.append(f'platform_worker_up{{role="{role}"}} {int(alive)}')
    return Response('\n'.join(lines)+'\n',media_type='text/plain; version=0.0.4',headers={'Cache-Control':'no-store'})


@router.get('/api/v1/admin/health-summary')
def health_summary(request:Request,db=Depends(get_db),user=Depends(admin_user)):
    pending=db.scalar(select(func.count()).select_from(Job).where(Job.status=='queued'))
    oldest=db.scalar(select(func.min(Job.created_at)).where(Job.status=='queued'))
    failed=db.scalar(select(func.count()).select_from(Job).where(Job.status=='failed',Job.finished_at>now()-timedelta(hours=1)))
    mail=db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.sent_at.is_(None)))
    age=max(0,(now()-oldest).total_seconds()) if oldest else 0
    allocated=db.scalar(select(func.coalesce(func.sum(Space.used_bytes+Space.reserved_bytes),0)))
    cfg=request.app.state.settings
    dead_mail=db.scalar(select(func.count()).select_from(MailOutbox).where(MailOutbox.sent_at.is_(None),MailOutbox.attempts>=5))
    alerts=[]
    if allocated>=cfg.platform_storage_budget_bytes*.9:alerts.append({'severity':'warning','message':'平台逻辑存储预算已使用90%以上','action':'检查空间占用、过期产物和实际磁盘容量后调整预算'})
    if dead_mail:alerts.append({'severity':'warning','message':'存在已达到投递重试上限的邮件','action':'修复传输后重新发起所需安全邮件，不复用已过期验证码'})
    if age>300:alerts.append({'severity':'warning','message':'任务排队超过5分钟','action':'检查 Worker 心跳、组织并发预算和模型状态'})
    if failed:alerts.append({'severity':'warning','message':'最近一小时存在失败任务','action':'按失败原因排查并重试，不能把未检查标为通过'})
    if request.app.state.settings.mail_delivery_mode=='disabled':alerts.append({'severity':'info','message':'邮件服务尚未开通','action':'配置 SMTP 并使用收件确认探针验证'})
    return {'queued':pending,'oldest_queue_seconds':age,'failed_last_hour':failed,'mail_pending':mail,'mail_failed':dead_mail,
        'logical_storage_bytes':int(allocated),'logical_storage_budget_bytes':cfg.platform_storage_budget_bytes,'alerts':alerts,
        'scope':'shared-database','request_metrics_scope':'shared-redis' if request.app.state.http_metrics.redis else 'current-api-process'}
