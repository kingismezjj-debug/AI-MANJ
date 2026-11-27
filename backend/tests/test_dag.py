from pathlib import Path

from PIL import Image

from app.adapters.registry import default_provider_config, default_settings
from app.db.models import Character, Episode, Job, Location, Project, Scene, Shot
from app.media.ffmpeg_tools import still_to_mp4
from app.media.audio_synth import synth_wav
from app.pipeline.artifacts import save_bytes
from app.pipeline.graph import build_graph, kick
from app.pipeline.names import (
    CHARACTER_BIBLE,
    CHARACTER_LOCK,
    LOCATION_BIBLE,
    SHOT_QC,
    STORYBOARD,
    VIDEO,
    VOICE,
)
from app.pipeline.stages import QCRetry, run_shot_qc
from app.storage import get_store
from app.util import utcnow


def _job(db, project_id, stage, entity_id=None):
    query = db.query(Job).filter(Job.project_id == project_id, Job.stage == stage)
    if entity_id:
        query = query.filter(Job.entity_id == entity_id)
    return query.order_by(Job.created_at.desc()).first()


def seed_project(db, *, shots: int = 2, approval: bool = False) -> Project:
    project = Project(
        title="DAG",
        status="draft",
        phase="draft",
        provider_config=default_provider_config(),
        settings=default_settings(require_bible_approval=approval),
    )
    db.add(project)
    db.flush()
    screen = Job(
        project_id=project.id,
        stage="screenwrite",
        status="succeeded",
        entity_type="project",
        entity_id=project.id,
        depends_on=[],
        idempotency_key=f"screenwrite:{project.id}",
        enqueued=True,
        finished_at=utcnow(),
        result={},
    )
    db.add(screen)
    db.flush()
    for name in ("林夏", "陈默"):
        db.add(
            Character(
                project_id=project.id,
                name=name,
                appearance=f"{name}的外观",
                description=name,
                seed=3,
                locked=not approval,
                status="approved" if not approval else "generated",
            )
        )
    db.add(Location(project_id=project.id, name="旧楼楼道", description="雨夜"))
    episode = Episode(project_id=project.id, number=1, title="雨夜", hook="钩子", cliffhanger="悬念", status="pending")
    db.add(episode)
    db.flush()
    scene = Scene(project_id=project.id, episode_id=episode.id, number=1, location_name="旧楼楼道", description="楼道")
    db.add(scene)
    db.flush()
    for number in range(1, shots + 1):
        db.add(
            Shot(
                project_id=project.id,
                episode_id=episode.id,
                scene_id=scene.id,
                number=number,
                description=f"镜头{number}",
                dialogue="你好",
                speaker="林夏",
                camera="近景",
                duration_sec=2.5,
                visual_prompt="雨夜近景",
                character_names=["林夏"],
                stage_status={},
                seed=number,
            )
        )
    db.flush()
    build_graph(db, project.id, screen.id)
    db.commit()
    return project


def _mark(db, job, status="succeeded"):
    job.status = status
    job.finished_at = utcnow()
    db.commit()


def test_character_lock_blocks_storyboards_until_approval(db):
    from app.pipeline.controls import approve_all
    from app.pipeline.executor import execute_job

    project = seed_project(db, approval=True)
    for job in db.query(Job).filter(Job.project_id == project.id, Job.stage.in_([CHARACTER_BIBLE, LOCATION_BIBLE])).all():
        _mark(db, job)
    kick(project.id)
    db.expire_all()
    lock = _job(db, project.id, CHARACTER_LOCK)
    assert lock.status == "queued"
    execute_job(lock.id)
    db.expire_all()
    lock = _job(db, project.id, CHARACTER_LOCK)
    assert lock.status == "awaiting_approval"
    storyboards = db.query(Job).filter(Job.project_id == project.id, Job.stage == STORYBOARD).all()
    assert storyboards
    assert all(job.status == "blocked" for job in storyboards)
    approve_all(db, project.id)
    db.expire_all()
    execute_job(_job(db, project.id, CHARACTER_LOCK).id)
    db.expire_all()
    storyboards = db.query(Job).filter(Job.project_id == project.id, Job.stage == STORYBOARD).all()
    assert all(job.status == "queued" for job in storyboards)


def test_shot_video_starts_before_other_storyboards_finish(db):
    project = seed_project(db, shots=2, approval=False)
    for stage in (CHARACTER_BIBLE, LOCATION_BIBLE, CHARACTER_LOCK):
        for job in db.query(Job).filter(Job.project_id == project.id, Job.stage == stage).all():
            _mark(db, job)
    kick(project.id)
    db.expire_all()
    shots = db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.number).all()
    boards = [_job(db, project.id, STORYBOARD, shot.id) for shot in shots]
    assert all(job.status == "queued" for job in boards)
    videos = [_job(db, project.id, VIDEO, shot.id) for shot in shots]
    assert all(job.status == "blocked" for job in videos)
    _mark(db, boards[0])
    kick(project.id)
    db.expire_all()
    first_video = _job(db, project.id, VIDEO, shots[0].id)
    second_video = _job(db, project.id, VIDEO, shots[1].id)
    second_board = _job(db, project.id, STORYBOARD, shots[1].id)
    assert first_video.status == "queued"
    assert second_video.status == "blocked"
    assert second_board.status == "queued"
    assert first_video.depends_on == [boards[0].id]
    assert second_video.depends_on == [second_board.id]


def test_qc_retry_retargets_episode_assemble(db):
    project = seed_project(db, shots=1, approval=False)
    shot = db.query(Shot).filter(Shot.project_id == project.id).one()
    for stage in (STORYBOARD, VOICE):
        _mark(db, _job(db, project.id, stage, shot.id))
    store = get_store()
    frame = Image.new("RGB", (720, 1280), (20, 16, 24))
    frame_buf = __import__("io").BytesIO()
    frame.save(frame_buf, format="PNG")
    save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="keyframe",
        data=frame_buf.getvalue(),
        content_type="image/png",
        job_id=None,
    )
    folder = Path(store.root) / "qc"
    folder.mkdir(parents=True, exist_ok=True)
    image_path = folder / "f.png"
    audio_path = folder / "a.wav"
    video_path = folder / "v.mp4"
    image_path.write_bytes(frame_buf.getvalue())
    audio_path.write_bytes(synth_wav(2.0, freq=220))
    still_to_mp4(str(image_path), str(video_path), 2.0, 720, 1280, audio_path=str(audio_path))
    save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="lipsync",
        data=video_path.read_bytes(),
        content_type="video/mp4",
        job_id=None,
    )
    settings = dict(project.settings)
    settings["force_qc_fail_remaining"] = 1
    project.settings = settings
    qc = _job(db, project.id, SHOT_QC, shot.id)
    db.commit()
    try:
        run_shot_qc(db, qc, project)
        raise AssertionError("qc should retry")
    except QCRetry:
        pass
    db.commit()
    db.expire_all()
    shot = db.get(Shot, shot.id)
    assert shot.qc_attempts == 1
    regen = db.query(Job).filter(Job.idempotency_key == f"video:{shot.id}:g1").one()
    assert regen.status == "blocked"
    assemble = db.query(Job).filter(Job.project_id == project.id, Job.stage == "assemble_episode").one()
    new_qc = db.query(Job).filter(Job.idempotency_key == f"shot_qc:{shot.id}:g1").one()
    assert new_qc.id in assemble.depends_on
    assert qc.id not in assemble.depends_on
