"""Upstream tasks that never answer.

A timeout does not fail the job on the first miss. The job sits in 观察中,
then 待核实, and is polled again. A re-poll does not create a second model
charge. If it finally fails, the held amount for that job is returned once.
"""

class UpstreamPending(Exception):
    def __init__(self, task_id: str, held_usd: float = 0):
        super().__init__(task_id)
        self.task_id = task_id
        self.held_usd = held_usd


from app.billing import _entry
from app.db.models import Job, Project, User


def note_pending(job: Job, task_id: str, held_usd: float = 0) -> None:
    result = dict(job.result or {})
    result["upstream_task_id"] = task_id
    result["polls"] = int(result.get("polls") or 0)
    if held_usd and not result.get("held_usd"):
        result["held_usd"] = held_usd
    job.result = result
    job.status = "observing"
    job.enqueued = False
    job.error = "上游仍在生成，观察中"


def reap(db, project_id: str | None = None) -> list[dict]:
    query = db.query(Job).filter(Job.status.in_(["observing", "verifying"]))
    if project_id:
        query = query.filter(Job.project_id == project_id)
    report = []
    for job in query.all():
        result = dict(job.result or {})
        polls = int(result.get("polls") or 0) + 1
        result["polls"] = polls
        job.result = result
        if polls >= 5:
            job.status = "failed"
            job.error = "上游任务未能核实"
            refund_job(db, job)
            state = "failed"
        elif polls >= 3:
            job.status = "verifying"
            job.error = "待核实"
            state = "verifying"
        else:
            job.status = "observing"
            job.error = "观察中"
            state = "observing"
        report.append({"id": job.id, "status": state, "polls": polls, "task_id": result.get("upstream_task_id")})
    db.commit()
    return report


def refund_job(db, job: Job) -> float:
    result = dict(job.result or {})
    if result.get("refunded"):
        return 0.0
    amount = float(result.get("held_usd") or 0)
    project = db.get(Project, job.project_id)
    user = db.get(User, project.user_id) if project and project.user_id else None
    if user is not None and amount > 0:
        _entry(db, user, job.project_id, "refund", amount, "上游失败，退回这一笔")
    result["refunded"] = True
    job.result = result
    return amount


def list_stuck(db) -> list[dict]:
    jobs = db.query(Job).filter(Job.status.in_(["observing", "verifying"])).all()
    rows = []
    for job in jobs:
        result = job.result or {}
        rows.append(
            {
                "id": job.id,
                "project_id": job.project_id,
                "stage": job.stage,
                "status": job.status,
                "error": job.error,
                "task_id": result.get("upstream_task_id") or "",
                "polls": result.get("polls") or 0,
            }
        )
    return rows
