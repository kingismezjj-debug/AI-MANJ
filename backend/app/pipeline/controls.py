"""Human checkpoints and manual regeneration.

Character lock is the hard gate. Approval only releases the lock job; it does
not generate images. Shot regeneration inserts a new video chain and retargets
the episode assemble job, which is still blocked at that point.
"""

from app.db.models import Character, Job, Project, Shot
from app.db.session import SessionLocal
from app.pipeline.graph import kick
from app.pipeline.names import CHARACTER_LOCK, SCRIPT_LOCK, STORYBOARD


class ControlError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def approve_character(db, character_id: str) -> Character:
    character = db.get(Character, character_id)
    if character is None:
        raise ControlError("角色不存在", 404)
    character.locked = True
    character.status = "approved"
    db.commit()
    _release_lock_if_ready(character.project_id)
    return character


def approve_all(db, project_id: str) -> int:
    characters = db.query(Character).filter(Character.project_id == project_id).all()
    if not characters:
        raise ControlError("还没有角色", 404)
    for character in characters:
        character.locked = True
        character.status = "approved"
    db.commit()
    _release_lock_if_ready(project_id)
    return len(characters)


def _release_lock_if_ready(project_id: str) -> None:
    db = SessionLocal()
    try:
        characters = db.query(Character).filter(Character.project_id == project_id).all()
        if not characters or not all(item.locked for item in characters):
            return
        lock = db.query(Job).filter(Job.project_id == project_id, Job.stage == CHARACTER_LOCK).one_or_none()
        project = db.get(Project, project_id)
        if lock is None or lock.status != "awaiting_approval":
            return
        lock.status = "queued"
        lock.enqueued = False
        lock.error = None
        if project is not None and project.status == "awaiting_approval":
            project.status = "running"
        db.commit()
        kick(project_id)
    finally:
        db.close()


def regenerate_character(db, character_id: str) -> Job:
    character = db.get(Character, character_id)
    if character is None:
        raise ControlError("角色不存在", 404)
    project = db.get(Project, character.project_id)
    started = (
        db.query(Job)
        .filter(Job.project_id == project.id, Job.stage == STORYBOARD, Job.status.in_(["running", "succeeded"]))
        .count()
    )
    if started:
        raise ControlError("分镜已开始，定妆不能再改。请新建项目。")
    from app.pipeline.graph import create_job
    from app.pipeline.names import CHARACTER_BIBLE, SCREENWRITE

    screen = db.query(Job).filter(Job.project_id == project.id, Job.stage == SCREENWRITE).one()
    previous = db.query(Job).filter(Job.entity_id == character.id, Job.stage == CHARACTER_BIBLE).count()
    character.locked = False
    character.status = "pending"
    character.reference_key = ""
    job = create_job(
        db,
        project_id=project.id,
        stage=CHARACTER_BIBLE,
        entity_type="character",
        entity_id=character.id,
        depends_on=[screen.id],
        key=f"character_bible:{character.id}:r{previous}",
    )
    lock = db.query(Job).filter(Job.project_id == project.id, Job.stage == CHARACTER_LOCK).one_or_none()
    if lock is not None:
        lock.depends_on = list(lock.depends_on or []) + [job.id]
        lock.status = "blocked"
        lock.enqueued = False
    project.status = "running"
    db.commit()
    kick(project.id)
    db.refresh(job)
    return job


def approve_script(db, project_id: str) -> None:
    project = db.get(Project, project_id)
    if project is None:
        raise ControlError("项目不存在", 404)
    settings = dict(project.settings or {})
    settings["script_approved"] = True
    project.settings = settings
    lock = db.query(Job).filter(Job.project_id == project_id, Job.stage == SCRIPT_LOCK).one_or_none()
    if lock is not None and lock.status == "awaiting_approval":
        lock.status = "queued"
        lock.enqueued = False
        lock.error = None
    if project.status == "awaiting_script":
        project.status = "running"
    db.commit()
    kick(project_id)


def regenerate_shot(db, shot_id: str, stage: str = "shot", prompt: str | None = None, dialogue: str | None = None) -> None:
    from app.pipeline.regen import regenerate_shot_stage

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise ControlError("镜头不存在", 404)
    project = db.get(Project, shot.project_id)
    regenerate_shot_stage(db, shot, stage, prompt=prompt, dialogue=dialogue)
    if project.status in {"failed", "completed", "awaiting_script", "awaiting_approval"}:
        project.status = "running"
        project.error = None
    db.commit()
    kick(project.id)


def start_run(db, project: Project) -> None:
    from app.db.models import Novel

    novel = db.query(Novel).filter(Novel.project_id == project.id).one_or_none()
    if novel is None or not (novel.text or "").strip():
        raise ControlError("请先上传小说", 400)
    if project.status == "draft":
        from app.pipeline.graph import ensure_ingest_job

        job = ensure_ingest_job(db, project.id)
        job.status = "queued"
        job.enqueued = False
        project.status = "running"
        project.phase = "ingest"
        project.error = None
        db.commit()
        kick(project.id)
        return
    if project.status in {"failed", "interrupted"}:
        failed = db.query(Job).filter(Job.project_id == project.id, Job.status.in_(["failed", "interrupted"])).all()
        succeeded = {
            item.id for item in db.query(Job).filter(Job.project_id == project.id, Job.status == "succeeded").all()
        }
        for job in failed:
            if job.stage == "bible_refine" and not (project.settings or {}).get("full_read"):
                continue
            if job.stage in {"assemble_final", "qc_final"} and not (project.settings or {}).get("finish_all"):
                continue
            if job.stage == "shot_qc":
                continue
            if set(job.depends_on or []).issubset(succeeded):
                job.status = "queued"
                job.enqueued = False
                job.attempt = 0
                job.error = None
        project.status = "running"
        project.error = None
        db.commit()
        kick(project.id)
        return
    if project.status == "running":
        raise ControlError("项目正在制作")
    raise ControlError("项目当前状态不能开拍")
