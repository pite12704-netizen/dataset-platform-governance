"""Linux Celery 入口；任务结果持久化在 MySQL，不依赖 Redis result backend。"""
from .config import get_settings
from .tasks import celery_app
from celery.signals import beat_init, worker_ready
from .operations import start_presence

settings = get_settings()
app = celery_app(settings)


@worker_ready.connect
def worker_presence(sender=None, **kwargs):
    expected = {'vision':'platform.vision','mail':'platform.mail','worker':'celery',
                'retrieval':'platform.retrieval'}[settings.worker_role]
    queues = {q.name for q in sender.task_consumer.queues} if sender else set()
    if queues != {expected}:
        # 配错消费队列时不能报告健康，避免看似有视觉 Worker 却无人处理。
        raise RuntimeError('Worker 必须只消费其角色对应队列：' + expected)
    start_presence(settings, settings.worker_role)


@beat_init.connect
def beat_presence(**kwargs):
    start_presence(settings, "beat")
