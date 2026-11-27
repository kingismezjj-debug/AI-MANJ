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
    from datetime import timedelta

    from sqlalchemy import or_, update

    from app.config import get_settings

    stall = int(stall_seconds if stall_seconds is not None else get_settings().stall_seconds or 180)
    cutoff = utcnow() - timedelta(seconds=stall)
    changed = []
    for job in db.query(Job).filter(Job.status == "running").all():
        if not is_stale(job, stall):
            continue
        # A heartbeat committed after this read means the worker is still alive.
        updated = db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.status == "running",
                or_(
                    Job.heartbeat_at < cutoff,
                    (Job.heartbeat_at.is_(None) & (Job.started_at.is_(None) | (Job.started_at < cutoff))),
                ),
            )
            .values(status="interrupted", error="工人中断，可点继续", enqueued=False, finished_at=utcnow())
        )
        if updated.rowcount != 1:
            continue
        db.expire(job)
        project = db.get(Project, job.project_id)
        if project is not None and project.status == "running":
            project.status = "interrupted"
            project.error = "工人中断，可点继续"
            project.phase = (job.stage or "ingest")[:48]
        changed.append(job.id)
    if changed:
        db.commit()
    else:
        db.rollback()
    return changed


def beat_job(job_id: str, *, task_id: str | None = None, poll_path: str | None = None) -> None:
    """One short transaction. Safe to call from inside a long provider poll."""
    import logging

    from app.db.models import ModelCall
    from app.db.session import SessionLocal

    log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.heartbeat_at = utcnow()
        if task_id:
            result = dict(job.result or {})
            first = result.get("upstream_task_id") != task_id
            result["upstream_task_id"] = task_id
            if poll_path:
                result["video_poll_path"] = poll_path
            job.result = result
            if first:
                db.add(
                    ModelCall(
                        project_id=job.project_id,
                        job_id=job.id,
                        category="video",
                        provider="openai_compatible",
                        status="submitted",
                        cost_usd=0,
                        request_summary=f"task_id={task_id}",
                    )
                )
        db.commit()
    except Exception:
        db.rollback()
        log.exception("video heartbeat failed for %s", job_id)
    finally:
        db.close()
