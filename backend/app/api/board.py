from app.api.catalog import STAGE_LABELS
from app.pipeline.visual import as_dict
from app.api.costs import cost_report
from app.db.models import Artifact, Character, Episode, Job, Location, Project, Scene, Shot


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _latest_map(artifacts: list[Artifact]) -> dict[tuple[str, str], Artifact]:
    found: dict[tuple[str, str], Artifact] = {}
    for artifact in artifacts:
        key = (artifact.entity_id, artifact.kind)
        current = found.get(key)
        if current is None or artifact.created_at >= current.created_at:
            found[key] = artifact
    return found


def _url(artifact: Artifact | None) -> str | None:
    if artifact is None:
        return None
    return f"/api/artifacts/{artifact.id}/content"


def build_board(db, project: Project, *, offset: int = 0, limit: int = 80) -> dict:
    from sqlalchemy import func

    offset = max(0, int(offset))
    limit = min(80, max(1, int(limit)))
    episode_total = db.query(func.count(Episode.id)).filter(Episode.project_id == project.id).scalar() or 0
    episodes = (
        db.query(Episode)
        .filter(Episode.project_id == project.id)
        .order_by(Episode.number)
        .offset(offset)
        .limit(limit)
        .all()
    )
    episode_ids = [episode.id for episode in episodes]
    scenes = db.query(Scene).filter(Scene.episode_id.in_(episode_ids)).all() if episode_ids else []
    shots = (
        db.query(Shot).filter(Shot.episode_id.in_(episode_ids)).order_by(Shot.number).all() if episode_ids else []
    )
    characters = db.query(Character).filter(Character.project_id == project.id).all()
    locations = db.query(Location).filter(Location.project_id == project.id).all()
    shot_ids = [shot.id for shot in shots]
    jobs = (
        db.query(Job).filter(Job.project_id == project.id, Job.entity_id.in_(shot_ids)).all() if shot_ids else []
    )
    entity_ids = shot_ids + [item.id for item in characters] + [item.id for item in locations] + [project.id]
    artifacts = (
        db.query(Artifact)
        .filter(Artifact.project_id == project.id, Artifact.entity_id.in_(entity_ids))
        .all()
        if entity_ids
        else []
    )
    latest = _latest_map(artifacts)
    scene_by_id = {scene.id: scene for scene in scenes}

    def shot_payload(shot: Shot) -> dict:
        stage_jobs = {}
        for stage in ("storyboard", "tts_temp", "animatic", "video", "voice", "lipsync", "shot_qc"):
            matched = [job for job in jobs if job.stage == stage and job.entity_id == shot.id]
            if matched:
                job = matched[-1]
                stage_jobs[stage] = job.status
            else:
                stage_jobs[stage] = (shot.stage_status or {}).get(stage, "blocked")
        keyframe = latest.get((shot.id, "keyframe"))
        video = latest.get((shot.id, "video"))
        preview = latest.get((shot.id, "lipsync")) or latest.get((shot.id, "animatic")) or video
        scene = scene_by_id.get(shot.scene_id)
        return {
            "id": shot.id,
            "number": shot.number,
            "description": shot.description,
            "dialogue": shot.dialogue,
            "speaker": shot.speaker,
            "camera": shot.camera,
            "duration_sec": shot.duration_sec,
            "scene_number": scene.number if scene else 1,
            "location": scene.location_name if scene else "",
            "stages": stage_jobs,
            "thumbnail_url": _url(keyframe),
            "preview_url": _url(preview),
            "reference_count": (keyframe.meta or {}).get("reference_count") if keyframe else 0,
            "notice": ((video.meta or {}).get("notice") if video else "") or "",
        }

    episode_payloads = []
    for episode in episodes:
        episode_shots = [shot_payload(shot) for shot in shots if shot.episode_id == episode.id]
        episode_payloads.append(
            {
                "id": episode.id,
                "number": episode.number,
                "title": episode.title,
                "hook": episode.hook,
                "cliffhanger": episode.cliffhanger,
                "synopsis": episode.synopsis,
                "status": episode.status,
                "shots": episode_shots,
            }
        )
    counts = {
        status: count
        for status, count in db.query(Job.status, func.count(Job.id))
        .filter(Job.project_id == project.id)
        .group_by(Job.status)
        .all()
    }
    running = (
        db.query(Job)
        .filter(Job.project_id == project.id, Job.status == "running")
        .order_by(Job.updated_at.desc())
        .first()
    )
    activity = None
    if running is not None:
        activity = {
            "stage": running.stage,
            "label": STAGE_LABELS.get(running.stage, running.stage),
            "entity_type": running.entity_type,
            "entity_id": running.entity_id,
        }
    from app.pipeline.progress import snapshot

    progress = snapshot(db, project)
    for episode in episode_payloads:
        episode_shots = episode["shots"]
        pictured = sum(1 for shot in episode_shots if (shot["stages"].get("storyboard") == "succeeded"))
        if episode_shots:
            episode["progress"] = f"镜头 {pictured}/{len(episode_shots)} 出图"
        elif episode["status"] == "outlined":
            episode["progress"] = "已列大纲，还没写剧本"
        else:
            episode["progress"] = ""
    final = latest.get((project.id, "final_video"))
    subtitle = latest.get((project.id, "subtitle"))
    return {
        "project": {
            "id": project.id,
            "title": project.title,
            "status": project.status,
            "phase": project.phase,
            "style": project.style,
            "aspect_ratio": project.aspect_ratio,
            "width": project.width,
            "height": project.height,
            "error": project.error,
            "final_video_url": _url(final),
            "subtitle_url": _url(subtitle),
            "final_meta": final.meta if final else {},
            "created_at": _iso(project.created_at),
        },
        "episodes": episode_payloads,
        "episode_total": episode_total,
        "episode_offset": offset,
        "characters": [
            {
                "id": character.id,
                "name": character.name,
                "description": character.description,
                "appearance": character.appearance,
                "voice_hint": character.voice_hint,
                "anchors": as_dict(character.anchors),
                "seed": character.seed,
                "status": character.status,
                "locked": character.locked,
                "image_url": _url(latest.get((character.id, "character_ref"))),
            }
            for character in characters
        ],
        "locations": [
            {
                "id": location.id,
                "name": location.name,
                "description": location.description,
                "status": location.status,
                "image_url": _url(latest.get((location.id, "location_ref"))),
            }
            for location in locations
        ],
        "counts": counts,
        "activity": activity,
        "progress": progress,
        "recent_jobs": [
            {
                "id": job.id,
                "stage": job.stage,
                "label": STAGE_LABELS.get(job.stage, job.stage),
                "status": job.status,
                "entity_type": job.entity_type,
                "entity_id": job.entity_id,
                "error": job.error,
                "started_at": _iso(job.started_at),
                "finished_at": _iso(job.finished_at),
            }
            for job in db.query(Job)
            .filter(Job.project_id == project.id)
            .order_by(Job.updated_at.desc())
            .limit(16)
            .all()
        ],
        "costs": cost_report(db, project),
    }
