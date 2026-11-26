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


def build_board(db, project: Project) -> dict:
    episodes = db.query(Episode).filter(Episode.project_id == project.id).order_by(Episode.number).all()
    scenes = db.query(Scene).filter(Scene.project_id == project.id).all()
    shots = db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.number).all()
    characters = db.query(Character).filter(Character.project_id == project.id).all()
    locations = db.query(Location).filter(Location.project_id == project.id).all()
    jobs = db.query(Job).filter(Job.project_id == project.id).order_by(Job.created_at).all()
    artifacts = db.query(Artifact).filter(Artifact.project_id == project.id).all()
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
        preview = latest.get((shot.id, "lipsync")) or latest.get((shot.id, "animatic")) or latest.get((shot.id, "video"))
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
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    running = next((job for job in reversed(jobs) if job.status == "running"), None)
    activity = None
    if running is not None:
        activity = {
            "stage": running.stage,
            "label": STAGE_LABELS.get(running.stage, running.stage),
            "entity_type": running.entity_type,
            "entity_id": running.entity_id,
        }
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
            for job in sorted(jobs, key=lambda item: item.updated_at or item.created_at, reverse=True)[:16]
        ],
        "costs": cost_report(db, project),
    }
