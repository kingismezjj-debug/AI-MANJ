"""Manual regeneration of one shot, or one stage of one shot.

Other shots stay where they are. Downstream stages of the chosen shot are
rebuilt and the episode assemble job is pointed at the new QC result.
"""

from app.db.models import Job, Shot
from app.pipeline.graph import create_job
from app.pipeline.names import (
    ANIMATIC,
    ASSEMBLE_EPISODE,
    ASSEMBLE_FINAL,
    CHARACTER_LOCK,
    LIPSYNC,
    QC_FINAL,
    SCRIPT_LOCK,
    SCREENWRITE,
    SHOT_QC,
    STORYBOARD,
    TTS_TEMP,
    VIDEO,
    VOICE,
)
from app.pipeline.visual import apply_shot_prompt

STAGE_SETS = {
    "shot": {STORYBOARD, TTS_TEMP, VOICE, ANIMATIC, VIDEO, LIPSYNC, SHOT_QC},
    STORYBOARD: {STORYBOARD, ANIMATIC, VIDEO, LIPSYNC, SHOT_QC},
    TTS_TEMP: {TTS_TEMP, ANIMATIC},
    VOICE: {VOICE, LIPSYNC, SHOT_QC},
    VIDEO: {VIDEO, LIPSYNC, SHOT_QC},
    ANIMATIC: {ANIMATIC},
    LIPSYNC: {LIPSYNC, SHOT_QC},
}


def _latest_succeeded(db, stage: str, entity_id: str) -> Job | None:
    return (
        db.query(Job)
        .filter(Job.entity_id == entity_id, Job.stage == stage, Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .first()
    )


def _require_succeeded(db, stage: str, entity_id: str) -> Job:
    from app.pipeline.controls import ControlError

    job = _latest_succeeded(db, stage, entity_id)
    if job is None:
        raise ControlError("上游还没完成，不能从这一步重做", 409)
    return job


def _rewind(db, project_id: str, episode_id: str, new_qc_id: str | None, shot_id: str) -> None:
    if new_qc_id is None:
        return
    assemble = (
        db.query(Job).filter(Job.project_id == project_id, Job.stage == ASSEMBLE_EPISODE, Job.entity_id == episode_id).one()
    )
    if assemble.status == "running":
        from app.pipeline.controls import ControlError

        raise ControlError("本集正在合成，等它结束再重做")
    old_qcs = {
        job.id
        for job in db.query(Job).filter(Job.entity_id == shot_id, Job.stage == SHOT_QC).all()
        if job.id != new_qc_id
    }
    assemble.depends_on = [dep for dep in (assemble.depends_on or []) if dep not in old_qcs] + [new_qc_id]
    assemble.status = "blocked"
    assemble.enqueued = False
    assemble.error = None
    final = db.query(Job).filter(Job.project_id == project_id, Job.stage == ASSEMBLE_FINAL).one_or_none()
    qc_final = db.query(Job).filter(Job.project_id == project_id, Job.stage == QC_FINAL).one_or_none()
    for job in (final, qc_final):
        if job is not None and job.status != "running":
            job.status = "blocked"
            job.enqueued = False
            job.error = None


def regenerate_shot_stage(db, shot: Shot, stage: str, prompt: str | None = None, dialogue: str | None = None) -> None:
    from app.pipeline.controls import ControlError

    chosen = STAGE_SETS.get(stage)
    if chosen is None:
        raise ControlError("不支持的工序", 400)
    if prompt:
        shot.visual_prompt = prompt.strip()
    if dialogue is not None:
        shot.dialogue = dialogue
    elif stage in {VOICE, TTS_TEMP} and prompt:
        shot.dialogue = prompt.strip()
    apply_shot_prompt(db, shot)
    shot.revision = int(shot.revision or 0) + 1
    revision = shot.revision
    project_id = shot.project_id
    made: dict[str, str] = {}

    def dep(name: str, stage_name: str) -> str:
        if name in chosen:
            return made[name]
        return _require_succeeded(db, stage_name, shot.id).id

    gate = _latest_succeeded(db, SCRIPT_LOCK, project_id) or _require_succeeded(db, SCREENWRITE, project_id)
    if STORYBOARD in chosen:
        lock = _require_succeeded(db, CHARACTER_LOCK, project_id)
        job = create_job(
            db,
            project_id=project_id,
            stage=STORYBOARD,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[lock.id],
            key=f"storyboard:{shot.id}:r{revision}",
        )
        made[STORYBOARD] = job.id
    if TTS_TEMP in chosen:
        job = create_job(
            db,
            project_id=project_id,
            stage=TTS_TEMP,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[gate.id],
            key=f"tts_temp:{shot.id}:r{revision}",
        )
        made[TTS_TEMP] = job.id
    if VOICE in chosen:
        job = create_job(
            db,
            project_id=project_id,
            stage=VOICE,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[gate.id],
            key=f"voice:{shot.id}:r{revision}",
        )
        made[VOICE] = job.id
    if VIDEO in chosen:
        job = create_job(
            db,
            project_id=project_id,
            stage=VIDEO,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[dep(STORYBOARD, STORYBOARD)],
            key=f"video:{shot.id}:r{revision}",
        )
        made[VIDEO] = job.id
    if ANIMATIC in chosen:
        job = create_job(
            db,
            project_id=project_id,
            stage=ANIMATIC,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[dep(STORYBOARD, STORYBOARD), dep(TTS_TEMP, TTS_TEMP)],
            key=f"animatic:{shot.id}:r{revision}",
        )
        made[ANIMATIC] = job.id
    if LIPSYNC in chosen:
        job = create_job(
            db,
            project_id=project_id,
            stage=LIPSYNC,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[dep(VIDEO, VIDEO), dep(VOICE, VOICE)],
            key=f"lipsync:{shot.id}:r{revision}",
        )
        made[LIPSYNC] = job.id
    if SHOT_QC in chosen:
        job = create_job(
            db,
            project_id=project_id,
            stage=SHOT_QC,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[dep(LIPSYNC, LIPSYNC)],
            key=f"shot_qc:{shot.id}:r{revision}",
            max_attempts=1,
        )
        made[SHOT_QC] = job.id
        _rewind(db, project_id, shot.episode_id, job.id, shot.id)
