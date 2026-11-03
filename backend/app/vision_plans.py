"""Durable admission of visual shards. Beat advances plans after worker restarts."""
from sqlalchemy import select

from .common import audit, fetch, require
from .db import now
from .models import Asset, Job, User, Version
from .quality_contract import media_manifest
from .job_payloads import hydrate_job


def advance(plan_id,database,settings):
    from .tasks import locked_job, authorized_job, queue_job
    from .schemas import JobInput
    from .vision import fingerprint
    from .review_templates import for_version
    try:
        with database.Session() as db:
            plan=locked_job(db,plan_id)
            if plan.kind!='vision_plan' or plan.status not in {'queued','running'}:return []
            user=fetch(db,User,plan.user_id);version=fetch(db,Version,plan.version_id)
            require(authorized_job(db,user,plan,settings) and version.status in {'draft','rejected'},'计划权限已撤销或版本冻结',409)
            require(plan.parameters['pipeline_fingerprint']==fingerprint(settings,for_version(version)),'模型配置变化，请创建新计划',409)
            assets=db.scalars(select(Asset).where(Asset.version_id==version.id)).all()
            require(media_manifest(assets)==plan.parameters['population_manifest'],'计划图片清单变化，请重新安排',409)
            parameters=dict(plan.parameters)
            children=db.scalars(select(Job).where(Job.id.in_(parameters['children']))).all() if parameters['children'] else []
            for child in children:hydrate_job(db,child,fields=('parameters',))
            failed=[j.id for j in children if j.status in {'failed','cancelled'}]
            completed=sum(len(j.parameters['input_assets']) for j in children if j.status=='completed')
            active=sum(j.status in {'queued','running'} for j in children)
            plan.result={'completed_images':completed,'total_images':len(parameters['asset_ids']),
                         'scheduled_images':parameters['cursor'],'failed_job_ids':failed}
            plan.progress=completed*100//max(1,len(parameters['asset_ids']))
            if failed:
                plan.status,plan.error,plan.finished_at='failed','请先处理失败分片，再重试计划',now()
                db.commit();return []
            created=[]
            slots=max(0,settings.vision_plan_active_shards-active)
            from sqlalchemy import func
            active_space=db.scalar(select(func.count()).select_from(Job).where(Job.space_id==plan.space_id,Job.kind=='vision',
                Job.status.in_({'queued','running'})))
            slots=min(slots,max(0,settings.space_max_active_vision_shards-active_space))
            size=parameters['shard_size']
            for _ in range(slots):
                start=parameters['cursor'];ids=parameters['asset_ids'][start:start+size]
                if not ids:break
                child=queue_job(db,user,JobInput(version_id=version.id,kind='vision',asset_ids=ids,
                    idempotency_key=f'vision-plan:{plan.id}:{start}'),settings)
                child.parameters={**child.parameters,'parent_plan_id':plan.id,'planned_images':len(ids)}
                parameters['children']=[*parameters['children'],child.id]
                parameters['cursor']+=len(ids);created.append(child.id)
            plan.parameters=parameters
            plan.status='completed' if completed==len(parameters['asset_ids']) else 'running'
            plan.finished_at=now() if plan.status=='completed' else None
            plan.lease_until=None
            if plan.status=='completed':audit(db,user,'vision_plan.completed',plan.id,plan.space_id,images=completed)
            db.commit()
            return created
    except Exception as exc:
        with database.Session() as db:
            plan=db.scalar(select(Job).where(Job.id==plan_id).with_for_update())
            if plan and plan.status in {'running','queued'}:
                plan.status,plan.finished_at='failed',now()
                plan.error=str(exc.detail) if hasattr(exc,'detail') else type(exc).__name__
                db.commit()
        return []
