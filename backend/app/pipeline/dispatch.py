"""Job transport.

The database is the source of truth. This module only delivers a job id to a
worker. Swap Celery for Temporal by replacing `enqueue` with a workflow signal;
stage functions stay the same.
"""

from app.config import get_settings

_override = None


def set_enqueue_override(fn):
    global _override
    _override = fn


def enqueue(job_id: str) -> None:
    if _override is not None:
        _override(job_id)
        return
    if get_settings().inline_jobs:
        from app.pipeline.executor import execute_job

        execute_job(job_id)
        return
    from app.workers.tasks import run_job_task

    run_job_task.delay(job_id)
