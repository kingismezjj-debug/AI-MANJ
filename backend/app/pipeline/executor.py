import logging

from sqlalchemy import update

from app.db.models import Job, Project, Shot
from app.db.session import SessionLocal
from app.pipeline.events import publish
from app.pipeline.graph import kick
from app.pipeline.stages import HoldForApproval, QCRetry, mark_shot, run_stage
from app.pipeline.stuck import UpstreamPending, note_pending
from app.util import utcnow

logger = logging.getLogger(__name__)


def execute_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="running", started_at=utcnow())
        )
        db.commit()
        if claimed.rowcount != 1:
            return
        job = db.get(Job, job_id)
        project = db.get(Project, job.project_id)
        try:
            result = run_stage(db, job)
        except HoldForApproval as exc:
            job.status = "awaiting_approval"
            job.error = str(exc)
            if project is not None:
                if getattr(exc, "kind", "bible") == "script":
                    project.status = "awaiting_script"
                    project.phase = "script_lock"
                elif getattr(exc, "kind", "") == "grid":
                    project.status = "awaiting_grid"
                    project.phase = "grid"
                else:
                    project.status = "awaiting_approval"
                    project.phase = "character_lock"
            db.commit()
            publish(job.project_id, {"type": project.status if project else "awaiting_approval", "job_id": job.id})
            return
        except UpstreamPending as exc:
            note_pending(job, exc.task_id, exc.held_usd)
            db.commit()
            publish(job.project_id, {"type": "observing", "job_id": job.id})
            return
        except QCRetry as exc:
            job.status = "failed"
            job.error = str(exc)[:1000]
            job.finished_at = utcnow()
            db.commit()
            publish(job.project_id, {"type": "qc_retry", "job_id": job.id, "shot_id": job.entity_id})
            kick(job.project_id)
            return
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            project = db.get(Project, job.project_id) if job is not None else None
            if job is not None:
                _fail_or_retry(db, job, project, exc)
            return
        job.status = "succeeded"
        job.result = result or {}
        job.finished_at = utcnow()
        job.error = None
        db.commit()
        publish(
            job.project_id,
            {"type": "job_succeeded", "job_id": job.id, "stage": job.stage, "entity_id": job.entity_id},
        )
        kick(job.project_id)
    finally:
        db.close()


def _fail_or_retry(db, job: Job, project: Project | None, exc: Exception) -> None:
    job.attempt += 1
    message = str(exc)[:1000]
    job.error = message
    if job.entity_type == "shot":
        shot = db.get(Shot, job.entity_id)
        if shot is not None:
            mark_shot(shot, job.stage, "failed")
    if job.attempt < job.max_attempts:
        job.status = "queued"
        job.enqueued = False
        db.commit()
        from app.pipeline.dispatch import enqueue

        enqueue(job.id)
        return
    job.status = "failed"
    job.finished_at = utcnow()
    if project is not None:
        project.status = "failed"
        project.error = f"{job.stage}: {message}"
        from app.billing import settle_project

        settle_project(db, project)
    db.commit()
    publish(job.project_id, {"type": "job_failed", "job_id": job.id, "stage": job.stage, "error": message})
