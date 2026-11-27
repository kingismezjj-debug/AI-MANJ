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


def execute_job(job_id: str) -> str:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("run_job %s did no work: job missing", job_id)
            db.rollback()
            return "missing"
        from app.config import get_settings
        from app.pipeline.reclaim import is_stale, mark_interrupted

        stall = int(get_settings().stall_seconds or 180)
        if job.status == "running" and is_stale(job, stall):
            logger.warning("run_job %s did no work as a live task: stale running %s, marking interrupted", job_id, job.stage)
            mark_interrupted(db, job)
            db.commit()
            publish(job.project_id, {"type": "interrupted", "job_id": job.id, "stage": job.stage})
            return "interrupted"
        if job.status != "queued":
            logger.warning("run_job %s did no work: status=%s stage=%s", job_id, job.status, job.stage)
            db.rollback()
            return f"skip:{job.status}"
        claimed = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "queued")
            .values(status="running", started_at=utcnow(), heartbeat_at=utcnow())
        )
        db.commit()
        if claimed.rowcount != 1:
            job = db.get(Job, job_id)
            logger.warning("run_job %s did no work: claim lost, status=%s", job_id, job.status if job else None)
            db.rollback()
            return "claim-lost"
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
            return "held"
        except UpstreamPending as exc:
            note_pending(job, exc.task_id, exc.held_usd)
            db.commit()
            publish(job.project_id, {"type": "observing", "job_id": job.id})
            return "observing"
        except QCRetry as exc:
            job.status = "failed"
            job.error = str(exc)[:1000]
            job.finished_at = utcnow()
            db.commit()
            publish(job.project_id, {"type": "qc_retry", "job_id": job.id, "shot_id": job.entity_id})
            kick(job.project_id)
            return "qc-retry"
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            db.rollback()
            job = db.get(Job, job_id)
            project = db.get(Project, job.project_id) if job is not None else None
            if job is not None:
                _fail_or_retry(db, job, project, exc)
            return "failed"
        db.refresh(job)
        prior = dict(job.result or {})
        prior.update(result or {})
        job.status = "succeeded"
        job.result = prior
        job.finished_at = utcnow()
        job.heartbeat_at = utcnow()
        job.error = None
        if project is not None:
            db.refresh(project)
            if project.status == "interrupted" and (project.error or "").startswith("工人中断"):
                project.status = "running"
                project.error = None
        db.commit()
        publish(
            job.project_id,
            {"type": "job_succeeded", "job_id": job.id, "stage": job.stage, "entity_id": job.entity_id},
        )
        kick(job.project_id)
        return "ran"
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
