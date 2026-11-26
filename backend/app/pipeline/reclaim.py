"""Jobs left running by a dead worker.

A rebuild acks the task late and delivers it again. If we only look for
status=queued, that delivery exits in a tenth of a second and the project
stays 制作中 forever. A running job with an old heartbeat is interrupted
instead, and 继续 queues the same stage again.
"""

from datetime import timezone

from app.db.models import Job, Project
from app.util import utcnow


def as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_stale(job: Job, stall_seconds: int, now=None) -> bool:
    if job.status != "running":
        return False
    beat = as_utc(job.heartbeat_at or job.started_at)
    if beat is None:
        return True
    moment = now or utcnow()
    return (moment - beat).total_seconds() >= stall_seconds


def mark_interrupted(db, job: Job, reason: str = "工人中断，可点继续") -> None:
    job.status = "interrupted"
    job.error = reason[:1000]
    job.enqueued = False
    job.finished_at = utcnow()
    project = db.get(Project, job.project_id)
    if project is not None and project.status == "running":
        project.status = "interrupted"
        project.error = job.error
        project.phase = (job.stage or "ingest")[:48]


def reclaim_stale(db, stall_seconds: int | None = None) -> list[str]:
    from app.config import get_settings

    stall = int(stall_seconds if stall_seconds is not None else get_settings().stall_seconds or 180)
    changed = []
    for job in db.query(Job).filter(Job.status == "running").all():
        if not is_stale(job, stall):
            continue
        mark_interrupted(db, job)
        changed.append(job.id)
    if changed:
        db.commit()
    return changed
