from celery import Celery
from celery.signals import worker_ready

from app.bootstrap import init_database, init_storage
from app.config import get_settings

settings = get_settings()
celery_app = Celery("zhenchuan", broker=settings.redis_url or "redis://localhost:6379/0", backend=settings.redis_url or None)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="zhenchuan",
    broker_connection_retry_on_startup=True,
)


@worker_ready.connect
def _on_ready(**_kwargs):
    init_database()
    init_storage()


import app.workers.tasks  # noqa: E402,F401  — register tasks
