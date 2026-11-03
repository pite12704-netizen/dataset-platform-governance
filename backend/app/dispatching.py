"""Durable producer leases: bound duplicate messages without claiming exactly-once."""
from datetime import timedelta
import logging

from sqlalchemy import and_, or_, select

from .db import now, uid
from .models import Job

logger = logging.getLogger(__name__)


def eligible():
    return and_(Job.attempts < 3, or_(Job.status == 'queued',
        and_(Job.status == 'running', Job.lease_until < now()),
        and_(Job.status == 'running', Job.kind == 'vision_plan')),
        or_(Job.next_dispatch_at.is_(None), Job.next_dispatch_at <= now()))


def send(job_id, database, settings, broker):
    with database.Session() as db:
        job = db.scalar(select(Job).where(Job.id == job_id, eligible())
                        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if job is None:
            return False
        interval = (30 if job.kind == 'vision_plan' else min(settings.dispatch_max_backoff_seconds,
            settings.dispatch_lease_seconds * 2 ** min(job.dispatch_attempts, 6)))
        job.dispatch_token = uid()
        job.dispatch_attempts = min(job.dispatch_attempts + 1, 1000000)
        job.next_dispatch_at = now() + timedelta(seconds=interval)
        queue = 'platform.vision' if job.kind == 'vision' else 'celery'
        db.commit()  # A crash or uncertain broker acknowledgement is retried after this deadline.
    try:
        broker.send_task('dataset.run_job', args=[job_id], queue=queue, retry=False)
    except Exception as exc:
        logger.warning('Task dispatch deferred: job=%s exception=%s', job_id, type(exc).__name__)
        return False
    return True
