from sqlalchemy.orm import Session

from app.db.models import Character, Episode, Job, Location, Shot
from app.db.session import SessionLocal
from app.pipeline.dispatch import enqueue
from app.pipeline.names import (
    ANIMATIC,
    ASSEMBLE_EPISODE,
    ASSEMBLE_FINAL,
    CHARACTER_BIBLE,
    CHARACTER_LOCK,
    DIRECTOR,
    INGEST,
    SCRIPT_LOCK,
    LIPSYNC,
    LOCATION_BIBLE,
    MUSIC,
    QC_FINAL,
    SCREENWRITE,
    SHOT_QC,
    STORYBOARD,
    TTS_TEMP,
    VIDEO,
    VOICE,
)


def create_job(
    db: Session,
    *,
    project_id: str,
    stage: str,
    entity_type: str,
    entity_id: str,
    depends_on: list[str],
    key: str,
    max_attempts: int = 2,
) -> Job:
    existing = db.query(Job).filter(Job.idempotency_key == key).one_or_none()
    if existing is not None:
        return existing
    job = Job(
        project_id=project_id,
        stage=stage,
        status="blocked",
        entity_type=entity_type,
        entity_id=entity_id,
        depends_on=list(depends_on),
        idempotency_key=key,
        max_attempts=max_attempts,
        enqueued=False,
        result={},
    )
    db.add(job)
    db.flush()
    return job


def build_graph(db: Session, project_id: str, screen_job_id: str) -> None:
    """Fan out per-character bible jobs and per-shot jobs.

    Storyboard and video wait on character_lock. Audio, temp TTS, and music do
    not, so they can run while reference sheets are still rendering. Each shot's
    video is released as soon as that shot's storyboard exists.
    """
    characters = db.query(Character).filter(Character.project_id == project_id).all()
    locations = db.query(Location).filter(Location.project_id == project_id).all()
    episodes = db.query(Episode).filter(Episode.project_id == project_id).order_by(Episode.number).all()
    shots = db.query(Shot).filter(Shot.project_id == project_id).all()
    script_lock = create_job(
        db,
        project_id=project_id,
        stage=SCRIPT_LOCK,
        entity_type="project",
        entity_id=project_id,
        depends_on=[screen_job_id],
        key=f"script_lock:{project_id}",
        max_attempts=1,
    )

    char_jobs = [
        create_job(
            db,
            project_id=project_id,
            stage=CHARACTER_BIBLE,
            entity_type="character",
            entity_id=character.id,
            depends_on=[script_lock.id],
            key=f"character_bible:{character.id}",
        )
        for character in characters
    ]
    for location in locations:
        create_job(
            db,
            project_id=project_id,
            stage=LOCATION_BIBLE,
            entity_type="location",
            entity_id=location.id,
            depends_on=[script_lock.id],
            key=f"location_bible:{location.id}",
        )
    lock_deps = [job.id for job in char_jobs] or [screen_job_id]
    lock = create_job(
        db,
        project_id=project_id,
        stage=CHARACTER_LOCK,
        entity_type="project",
        entity_id=project_id,
        depends_on=lock_deps,
        key=f"character_lock:{project_id}",
        max_attempts=1,
    )
    episode_assembles: list[str] = []
    for episode in episodes:
        episode_shots = [shot for shot in shots if shot.episode_id == episode.id]
        episode_shots.sort(key=lambda shot: shot.number)
        if not episode_shots:
            continue
        qc_ids: list[str] = []
        for shot in episode_shots:
            storyboard = create_job(
                db,
                project_id=project_id,
                stage=STORYBOARD,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[lock.id],
                key=f"storyboard:{shot.id}",
            )
            tts = create_job(
                db,
                project_id=project_id,
                stage=TTS_TEMP,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[script_lock.id],
                key=f"tts_temp:{shot.id}",
            )
            voice = create_job(
                db,
                project_id=project_id,
                stage=VOICE,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[script_lock.id],
                key=f"voice:{shot.id}",
            )
            video = create_job(
                db,
                project_id=project_id,
                stage=VIDEO,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[storyboard.id],
                key=f"video:{shot.id}:g0",
            )
            create_job(
                db,
                project_id=project_id,
                stage=ANIMATIC,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[storyboard.id, tts.id],
                key=f"animatic:{shot.id}",
            )
            lipsync = create_job(
                db,
                project_id=project_id,
                stage=LIPSYNC,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[video.id, voice.id],
                key=f"lipsync:{shot.id}:g0",
            )
            qc = create_job(
                db,
                project_id=project_id,
                stage=SHOT_QC,
                entity_type="shot",
                entity_id=shot.id,
                depends_on=[lipsync.id],
                key=f"shot_qc:{shot.id}:g0",
                max_attempts=1,
            )
            qc_ids.append(qc.id)
        music = create_job(
            db,
            project_id=project_id,
            stage=MUSIC,
            entity_type="episode",
            entity_id=episode.id,
            depends_on=[script_lock.id],
            key=f"music:{episode.id}",
        )
        assemble = create_job(
            db,
            project_id=project_id,
            stage=ASSEMBLE_EPISODE,
            entity_type="episode",
            entity_id=episode.id,
            depends_on=qc_ids + [music.id],
            key=f"assemble_episode:{episode.id}",
            max_attempts=2,
        )
        episode_assembles.append(assemble.id)
    final = create_job(
        db,
        project_id=project_id,
        stage=ASSEMBLE_FINAL,
        entity_type="project",
        entity_id=project_id,
        depends_on=episode_assembles,
        key=f"assemble_final:{project_id}",
        max_attempts=2,
    )
    create_job(
        db,
        project_id=project_id,
        stage=QC_FINAL,
        entity_type="project",
        entity_id=project_id,
        depends_on=[final.id],
        key=f"qc_final:{project_id}",
        max_attempts=1,
    )


def release_ready(db: Session, project_id: str) -> None:
    jobs = db.query(Job).filter(Job.project_id == project_id).all()
    succeeded = {job.id for job in jobs if job.status == "succeeded"}
    for job in jobs:
        if job.status != "blocked":
            continue
        if set(job.depends_on or []).issubset(succeeded):
            job.status = "queued"
    db.commit()


def dispatch_queued(db: Session, project_id: str) -> None:
    pending = (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.status == "queued", Job.enqueued.is_(False))
        .all()
    )
    ids = [job.id for job in pending]
    for job in pending:
        job.enqueued = True
    db.commit()
    for job_id in ids:
        enqueue(job_id)


def kick(project_id: str) -> None:
    db = SessionLocal()
    try:
        release_ready(db, project_id)
        dispatch_queued(db, project_id)
    finally:
        db.close()


def schedule_video_regen(db: Session, shot: Shot) -> Job:
    """Queue a fresh video → lip-sync → QC chain and retarget the episode assemble."""
    generation = shot.qc_attempts
    storyboard = (
        db.query(Job)
        .filter(Job.entity_id == shot.id, Job.stage == STORYBOARD, Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .first()
    )
    voice = (
        db.query(Job)
        .filter(Job.entity_id == shot.id, Job.stage == VOICE, Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .first()
    )
    if storyboard is None or voice is None:
        raise RuntimeError("cannot regenerate video before storyboard and voice succeed")
    video = create_job(
        db,
        project_id=shot.project_id,
        stage=VIDEO,
        entity_type="shot",
        entity_id=shot.id,
        depends_on=[storyboard.id],
        key=f"video:{shot.id}:g{generation}",
    )
    lipsync = create_job(
        db,
        project_id=shot.project_id,
        stage=LIPSYNC,
        entity_type="shot",
        entity_id=shot.id,
        depends_on=[video.id, voice.id],
        key=f"lipsync:{shot.id}:g{generation}",
    )
    qc = create_job(
        db,
        project_id=shot.project_id,
        stage=SHOT_QC,
        entity_type="shot",
        entity_id=shot.id,
        depends_on=[lipsync.id],
        key=f"shot_qc:{shot.id}:g{generation}",
        max_attempts=1,
    )
    assemble = (
        db.query(Job)
        .filter(Job.project_id == shot.project_id, Job.stage == ASSEMBLE_EPISODE, Job.entity_id == shot.episode_id)
        .one()
    )
    old_qc_ids = {
        job.id
        for job in db.query(Job).filter(Job.entity_id == shot.id, Job.stage == SHOT_QC).all()
        if job.id != qc.id
    }
    assemble.depends_on = [dep for dep in (assemble.depends_on or []) if dep not in old_qc_ids] + [qc.id]
    if assemble.status in {"failed", "succeeded"}:
        assemble.status = "blocked"
        assemble.enqueued = False
    return video


def ensure_ingest_job(db: Session, project_id: str) -> Job:
    return create_job(
        db,
        project_id=project_id,
        stage=INGEST,
        entity_type="project",
        entity_id=project_id,
        depends_on=[],
        key=f"ingest:{project_id}",
    )


def ensure_director_job(db: Session, project_id: str, ingest_job_id: str) -> Job:
    return create_job(
        db,
        project_id=project_id,
        stage=DIRECTOR,
        entity_type="project",
        entity_id=project_id,
        depends_on=[ingest_job_id],
        key=f"director:{project_id}",
        max_attempts=2,
    )


def ensure_screen_job(db: Session, project_id: str, director_job_id: str) -> Job:
    return create_job(
        db,
        project_id=project_id,
        stage=SCREENWRITE,
        entity_type="project",
        entity_id=project_id,
        depends_on=[director_job_id],
        key=f"screenwrite:{project_id}",
    )
