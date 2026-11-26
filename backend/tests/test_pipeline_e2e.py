from app.db.models import Artifact, Job, Project, Shot
from app.media.ffmpeg_tools import duration_of, has_stream
from app.pipeline.executor import execute_job
from app.storage import get_store


def test_sample_novel_renders_final_mp4(db, client):
    from app.pipeline.dispatch import set_enqueue_override

    set_enqueue_override(execute_job)
    created = client.post("/api/projects", json={"title": "雨夜来信", "preset": "mock"}).json()
    project_id = created["id"]
    uploaded = client.post(f"/api/projects/{project_id}/novel/sample")
    assert uploaded.status_code == 200
    assert uploaded.json()["char_count"] > 40
    started = client.post(f"/api/projects/{project_id}/run")
    assert started.status_code == 200

    db.expire_all()
    project = db.get(Project, project_id)
    assert project.status == "completed", project.error
    final = db.get(Artifact, project.final_artifact_id)
    assert final is not None
    assert final.meta.get("burned_subtitles") is True
    assert final.meta.get("ai_label") is True
    path = get_store().root / final.storage_key
    assert duration_of(path) > 5
    assert has_stream(path, "video")
    assert has_stream(path, "audio")
    subtitle = (
        db.query(Artifact)
        .filter(Artifact.project_id == project_id, Artifact.kind == "subtitle")
        .one()
    )
    text = get_store().get(subtitle.storage_key).decode("utf-8")
    assert "三年了" in text
    assert "AI生成" in text
    keyframe = (
        db.query(Artifact)
        .filter(Artifact.project_id == project_id, Artifact.kind == "keyframe")
        .first()
    )
    assert keyframe.meta.get("reference_count", 0) >= 1
    failed = db.query(Job).filter(Job.project_id == project_id, Job.status == "failed").all()
    assert failed == []
    board = client.get(f"/api/projects/{project_id}").json()
    assert board["episodes"][0]["shots"]
    assert board["episodes"][0]["shots"][0]["thumbnail_url"]
    assert board["costs"]["actual_usd"] == 0
    assert board["costs"]["reference_usd"] > 0
    assert board["project"]["final_video_url"]
    prompted = db.query(Shot).filter(Shot.project_id == project_id).all()
    assert prompted
    assert all("口型" in (shot.video_prompt or "") for shot in prompted)
    assert all((shot.dialogue_timing or {}).get("end_sec", 0) >= 0 for shot in prompted)
