"""Authorized resumable state snapshots over SSE; never a durable event log."""
import asyncio,json,time
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import load_only
from .concurrency import admission
from .db import run_db
from .common import require
from .models import AgentAction,Job
from .security import resolve_interactive_user
from .tasks import authorized_job
from .quality_contract import digest

router=APIRouter(prefix='/api/v1/agent',tags=['Agent任务进度'])

def identity(db,request):return resolve_interactive_user(request,db).id

async def stream_identity(request:Request):
    user_id=await run_db(request,identity,request)
    async with admission(request.app.state.agent_status_limiter,'任务进度连接已满，请稍后重试'):
        owners=request.app.state.agent_status_owners
        require(owners.get(user_id,0)<2,'此账号的任务进度连接已满',429)
        owners[user_id]=owners.get(user_id,0)+1
        try:yield user_id
        finally:
            owners[user_id]-=1
            if not owners[user_id]:owners.pop(user_id,None)

def state_snapshot(db,request,user_id,action_id):
    user=resolve_interactive_user(request,db);require(user.id==user_id,'登录身份已变化',401)
    action=db.scalar(select(AgentAction).where(AgentAction.id==action_id,AgentAction.user_id==user.id))
    require(action is not None,'操作不存在',404)
    require(action.status=='completed' and action.result.get('job_id'),'尚未提交后台任务',409)
    job=db.scalar(select(Job).where(Job.id==action.result['job_id']).options(load_only(
        Job.id,Job.user_id,Job.version_id,Job.kind,Job.status,Job.progress,Job.attempts,Job.finished_at,Job.result)))
    require(job and authorized_job(db,user,job,request.app.state.settings),'任务授权已失效',403)
    require(job.version_id==action.version_id and job.user_id==user.id,'任务归属不一致',403)
    fields=('total','checked','failed','completed_images','total_images','scheduled_images','processed_files','total_files')
    progress=job.progress;basis=None
    if job.kind=='vision_plan' and job.status in {'queued','running'}:
        children=db.execute(select(Job.status,Job.progress,Job.parameters['planned_images'].as_integer())
            .where(Job.version_id==job.version_id,Job.kind=='vision',Job.parameters['parent_plan_id'].as_string()==job.id)).all()
        total=job.result.get('total_images') or action.summary.get('workflow',{}).get('images',0)
        if total and children:
            work=sum((int(size or 0)*(100 if status=='completed' else percent if status=='running' else 0)) for status,percent,size in children)
            progress=max(progress,min(99,work//total))
            basis='运行中百分比按分片热状态估算，完成图片数及最终状态以已提交结果为准'
    return {'action_id':action.id,'job_id':job.id,'kind':job.kind,'status':job.status,
        'progress':progress,'committed_progress':job.progress,'progress_basis':basis,
        'attempts':job.attempts,'finished_at':job.finished_at.isoformat() if job.finished_at else None,
        'counts':{k:v for k,v in job.result.items() if k in fields and type(v) in {int,float}},
        'recovery_hint':'请在任务中心处理失败/取消的分片，再重试此计划；已经成功的检查点可复用' if job.kind=='vision_plan' and job.status in {'failed','cancelled'} else None,
        'plan_hash':action.result.get('plan_hash'),'url':'/jobs','source':'persistent-job-snapshot'}

@router.get('/actions/{action_id}/status')
async def action_status(action_id:str,request:Request):
    user_id=await run_db(request,identity,request)
    return await run_db(request,state_snapshot,request,user_id,action_id)

@router.get('/actions/{action_id}/events')
async def action_events(action_id:str,request:Request,user_id=Depends(stream_identity)):
    initial=await run_db(request,state_snapshot,request,user_id,action_id)
    settings=request.app.state.settings
    async def events():
        previous=None;start=time.monotonic();heartbeat=start;first=True
        yield 'retry: 5000\n\n'
        while time.monotonic()-start<settings.agent_status_stream_seconds:
            if await request.is_disconnected():break
            try:
                value=initial if first else await run_db(request,state_snapshot,request,user_id,action_id)
                first=False
            except HTTPException as exc:
                event='access_lost' if exc.status_code in {401,403,404} else 'temporary_error'
                yield f'event: {event}\ndata: {{}}\n\n';break
            except Exception:
                yield 'event: temporary_error\ndata: {}\n\n';break
            identifier=digest(value)
            if identifier!=previous:
                yield f'id: {identifier}\nevent: snapshot\ndata: {json.dumps(value,ensure_ascii=False,separators=(",",":"))}\n\n'
                previous=identifier
            if value['status'] in {'completed','failed','cancelled'}:break
            if time.monotonic()-heartbeat>=10:
                yield ': heartbeat\n\n';heartbeat=time.monotonic()
            # No database session or transaction survives this wait.
            await asyncio.sleep(settings.agent_status_poll_seconds)
        else:yield 'event: reconnect\ndata: {}\n\n'
    return StreamingResponse(events(),media_type='text/event-stream',headers={
        'Cache-Control':'no-store, no-transform','X-Accel-Buffering':'no'})
