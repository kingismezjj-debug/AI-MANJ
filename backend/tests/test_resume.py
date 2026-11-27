from datetime import timedelta

from app.db.models import Job, ModelCall, Project
from app.pipeline.controls import start_run
from app.pipeline.executor import execute_job
from app.pipeline.longform import analysis_model, cached_note, map_chapter, store_note
from app.pipeline.reclaim import is_stale, reclaim_stale
from app.util import utcnow
from app.adapters.router import ModelRouter


def _job(project, **kwargs):
    status = kwargs.pop("status", "running")
    stage = kwargs.pop("stage", "ingest")
    entity_type = kwargs.pop("entity_type", "project")
    entity_id = kwargs.pop("entity_id", project.id)
    job = Job(
        project_id=project.id,
        stage=stage,
        status=status,
        entity_type=entity_type,
        entity_id=entity_id,
        depends_on=[],
        idempotency_key=kwargs.pop("key"),
        **kwargs,
    )
    return job


def test_stale_running_job_becomes_interrupted(db):
    project = Project(title="中断", status="running", phase="ingest", settings={})
    db.add(project)
    db.flush()
    job = _job(project, key="stale-1", started_at=utcnow() - timedelta(minutes=10))
    db.add(job)
    db.commit()
    assert is_stale(job, 180) is True
    found = reclaim_stale(db, stall_seconds=180)
    db.refresh(job)
    db.refresh(project)
    assert job.id in found
    assert job.status == "interrupted"
    assert project.status == "interrupted"
    assert "继续" in (project.error or "")


def test_reaper_leaves_a_job_alone_when_its_heartbeat_is_fresh(db, monkeypatch):
    project = Project(title="刚跳过", status="running", settings={})
    db.add(project)
    db.flush()
    job = _job(project, key="beat-1", started_at=utcnow() - timedelta(minutes=10), heartbeat_at=utcnow())
    db.add(job)
    db.commit()
    monkeypatch.setattr("app.pipeline.reclaim.is_stale", lambda *_args, **_kwargs: True)
    assert reclaim_stale(db, stall_seconds=180) == []
    db.refresh(job)
    assert job.status == "running"


def test_video_heartbeat_persists_the_paid_task(db):
    from app.pipeline.reclaim import beat_job

    project = Project(title="心跳", status="running", settings={})
    db.add(project)
    db.flush()
    job = _job(project, key="video-beat", status="running", stage="video", started_at=utcnow() - timedelta(minutes=4))
    db.add(job)
    db.commit()
    beat_job(job.id, task_id="task_paid", poll_path="/videos/{task_id}")
    beat_job(job.id, task_id="task_paid", poll_path="/videos/{task_id}")
    db.refresh(job)
    assert job.result["upstream_task_id"] == "task_paid"
    assert job.result["video_poll_path"] == "/videos/{task_id}"
    assert job.heartbeat_at > job.started_at
    assert db.query(ModelCall).filter(ModelCall.job_id == job.id, ModelCall.request_summary == "task_id=task_paid").count() == 1
    assert reclaim_stale(db, stall_seconds=180) == []


def test_successful_finish_overwrites_a_reaper_interrupt(db, monkeypatch):
    from app.db.session import SessionLocal
    from app.pipeline.reclaim import mark_interrupted

    project = Project(title="赶完", status="running", settings={})
    db.add(project)
    db.flush()
    job = _job(project, key="finish-1", status="queued", stage="video", entity_type="shot", entity_id="shot-1")
    db.add(job)
    db.commit()

    def finish(session, current):
        other = SessionLocal()
        try:
            row = other.get(Job, current.id)
            mark_interrupted(other, row)
            other.commit()
        finally:
            other.close()
        return {"artifact_id": "clip-1", "upstream_task_id": "task_paid"}

    monkeypatch.setattr("app.pipeline.executor.run_stage", finish)
    monkeypatch.setattr("app.pipeline.executor.kick", lambda *_args, **_kwargs: None)
    assert execute_job(job.id) == "ran"
    db.refresh(job)
    db.refresh(project)
    assert job.status == "succeeded"
    assert job.result["artifact_id"] == "clip-1"
    assert job.result["upstream_task_id"] == "task_paid"
    assert job.error is None
    assert project.status == "running"


def test_live_job_is_not_interrupted(db):
    project = Project(title="活着", status="running", settings={})
    db.add(project)
    db.flush()
    job = _job(project, key="live-1", started_at=utcnow(), heartbeat_at=utcnow())
    db.add(job)
    db.commit()
    assert reclaim_stale(db, stall_seconds=180) == []
    db.refresh(job)
    assert job.status == "running"


def test_redelivered_stale_job_does_not_leave_project_running(db, caplog):
    project = Project(title="空转", status="running", phase="ingest", settings={})
    db.add(project)
    db.flush()
    job = _job(project, key="noop-1", started_at=utcnow() - timedelta(minutes=20))
    db.add(job)
    db.commit()
    reason = execute_job(job.id)
    db.refresh(job)
    db.refresh(project)
    assert reason == "interrupted"
    assert job.status == "interrupted"
    assert project.status == "interrupted"
    assert "did no work" in caplog.text


def test_continue_requeues_interrupted_stage(db):
    from app.db.models import Novel

    project = Project(title="接着", status="interrupted", settings={"fast_start": False}, provider_config={})
    db.add(project)
    db.flush()
    db.add(Novel(project_id=project.id, filename="a.txt", text="有字", char_count=2))
    job = _job(project, key=f"ingest:{project.id}", status="interrupted", started_at=utcnow())
    db.add(job)
    db.commit()
    start_run(db, project)
    db.refresh(job)
    db.refresh(project)
    assert job.status == "queued"
    assert job.attempt == 0
    assert project.status == "running"


def test_chapter_note_is_reused(db):
    project = Project(title="缓存", status="running", settings={"use_global_models": True}, provider_config={})
    db.add(project)
    db.commit()
    text = "第1章\n林夏：缓存这一句。"
    calls_before = db.query(ModelCall).filter(ModelCall.project_id == project.id).count()
    first = map_chapter(ModelRouter(db, project), text, 0, db=db, project_id=project.id)
    db.commit()
    calls_after = db.query(ModelCall).filter(ModelCall.project_id == project.id).count()
    assert calls_after == calls_before + 1
    second = map_chapter(ModelRouter(db, project), text, 0, db=db, project_id=project.id)
    db.commit()
    assert db.query(ModelCall).filter(ModelCall.project_id == project.id).count() == calls_after
    assert second == first
    assert cached_note(db, project.id, text)["notes"] or second


def test_reasoner_maps_with_fast_chat_model():
    assert analysis_model({"provider": "openai_compatible", "model": "deepseek-reasoner", "params": {}}) == "deepseek-chat"
    assert analysis_model({"provider": "openai_compatible", "model": "deepseek-reasoner", "params": {"fast_model": "my-fast"}}) == "my-fast"
    assert analysis_model({"provider": "mock", "model": "mock-screenwriter", "params": {}}) is None


def test_store_note_roundtrip(db):
    project = Project(title="笔记", settings={})
    db.add(project)
    db.commit()
    store_note(db, project.id, "同一段", {"notes": "短"})
    db.commit()
    assert cached_note(db, project.id, "同一段") == {"notes": "短"}
