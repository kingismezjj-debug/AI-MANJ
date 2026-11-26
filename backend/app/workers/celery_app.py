import logging

from celery import Celery
from celery.signals import worker_ready

from app.bootstrap import init_database, init_storage
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
celery_app = Celery("zhenchuan", broker=settings.redis_url or "redis://localhost:6379/0", backend=settings.redis_url or None)
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="zhenchuan",
    broker_connection_retry_on_startup=True,
)


def _reclaim_loop() -> None:
    import time

    from app.db.session import SessionLocal
    from app.pipeline.reclaim import reclaim_stale

    while True:
        time.sleep(60)
        db = SessionLocal()
        try:
            found = reclaim_stale(db)
            if found:
                logger.info("marked %s stale jobs interrupted", len(found))
            else:
                db.rollback()
        except Exception:
            db.rollback()
            logger.exception("stale job reclaim failed")
        finally:
            db.close()


@worker_ready.connect
def _on_ready(**_kwargs):
    import threading

    from app.db.session import SessionLocal
    from app.pipeline.reclaim import reclaim_stale

    init_database()
    init_storage()
    db = SessionLocal()
    try:
        reclaim_stale(db)
        db.rollback()
    finally:
        db.close()
    threading.Thread(target=_reclaim_loop, name="reclaim-stale", daemon=True).start()


import app.workers.tasks  # noqa: E402,F401  — register tasks
