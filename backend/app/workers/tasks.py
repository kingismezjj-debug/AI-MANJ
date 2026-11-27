from app.workers.celery_app import celery_app


@celery_app.task(name="zhenchuan.run_job")
def run_job_task(job_id: str) -> None:
    from app.pipeline.executor import execute_job

    execute_job(job_id)
