"""Visible progress for a project that may sit inside one long job.

Chapter mapping updates a stored counter after every chunk. Later stages are
read from the job table. Old projects that never wrote this counter stay
indeterminate instead of pretending to be at zero.
"""

from datetime import datetime, timezone

WEIGHTS = (
    ("map", 40),
    ("reduce", 5),
    ("director", 5),
    ("script", 12),
    ("cast", 8),
    ("storyboard", 12),
    ("video", 10),
    ("finish", 8),
)


def bump(db, project, *, stage: str, label: str, done: int, total: int, job=None) -> None:
    from app.pipeline.events import publish
    from app.util import utcnow

    now = utcnow()
    settings = dict(project.settings or {})
    previous = dict(settings.get("progress") or {})
    started = previous.get("started_at") if previous.get("stage") == stage else now.isoformat()
    settings["progress"] = {
        "stage": stage,
        "label": label,
        "done": int(done),
        "total": max(1, int(total)),
        "started_at": started,
        "updated_at": now.isoformat(),
    }
    project.settings = settings
    project.phase = stage[:48]
    if job is not None:
        job.heartbeat_at = now
    db.commit()
    publish(project.id, {"type": "progress", "label": label, "done": done, "total": total})


def eta_seconds(done: int, total: int, latencies_ms: list[int], concurrency: int) -> int | None:
    if total <= 0 or done <= 0 or done >= total or not latencies_ms:
        return None
    average = sum(latencies_ms) / len(latencies_ms) / 1000
    remaining = total - done
    return max(1, int(remaining * average / max(1, concurrency)))


def _ratio(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, done / total))


def overall_percent(
    *,
    status: str,
    stored: dict | None,
    ingest_done: bool,
    director_done: bool,
    script_done: bool,
    cast_done: int,
    cast_total: int,
    storyboard_done: int,
    storyboard_total: int,
    video_done: int,
    video_total: int,
    finish_done: bool,
) -> float | None:
    """Weighted 0–100. None means the project predates progress tracking."""
    if status == "completed" or finish_done:
        return 100.0
    if stored is None and not ingest_done and status == "running":
        return None
    score = 0.0
    weights = dict(WEIGHTS)
    if stored and stored.get("stage") == "map" and not ingest_done:
        score += weights["map"] * _ratio(int(stored.get("done") or 0), int(stored.get("total") or 1))
    elif ingest_done or (stored and stored.get("stage") not in {None, "map"}):
        score += weights["map"] + weights["reduce"]
    elif stored and stored.get("stage") == "reduce":
        score += weights["map"] + weights["reduce"]
    if director_done:
        score += weights["director"]
    if script_done:
        score += weights["script"]
    elif stored and stored.get("stage") == "script":
        score += weights["script"] * _ratio(int(stored.get("done") or 0), int(stored.get("total") or 1))
    if cast_total:
        score += weights["cast"] * _ratio(cast_done, cast_total)
    if storyboard_total:
        score += weights["storyboard"] * _ratio(storyboard_done, storyboard_total)
    if video_total:
        score += weights["video"] * _ratio(video_done, video_total)
    if status == "failed":
        return round(min(score, 99.0), 1)
    return round(min(score, 99.0), 1)


def progress_view(
    *,
    status: str,
    error: str | None,
    stored: dict | None,
    now: datetime,
    last_call: dict | None,
    stall_seconds: int,
    jobs_exist: bool,
    label: str,
    percent: float | None,
    eta: int | None,
) -> dict:
    stamps = []
    if stored and stored.get("updated_at"):
        stamps.append(_parse(stored["updated_at"]))
    if last_call and last_call.get("at"):
        stamps.append(_parse(last_call["at"]))
    latest = max((item for item in stamps if item is not None), default=None)
    seconds_ago = int((now - latest).total_seconds()) if latest is not None else None
    stalled = bool(status == "running" and seconds_ago is not None and seconds_ago >= stall_seconds)
    state = "failed" if status == "failed" else ("stalled" if stalled else status)
    if status == "interrupted":
        label = "已中断"
        state = "interrupted"
    elif status == "failed":
        label = "出错"
    elif stalled:
        label = f"疑似卡住 · {label}"
    return {
        "state": state,
        "label": label,
        "percent": percent,
        "indeterminate": percent is None,
        "seconds_ago": seconds_ago,
        "eta_sec": None if status in {"completed", "failed"} else eta,
        "stalled": stalled,
        "error": error,
        "last_call": last_call,
        "jobs_exist": jobs_exist,
    }


def snapshot(db, project) -> dict:
    from sqlalchemy import func

    from app.config import get_settings
    from app.db.models import Job, ModelCall, Shot
    from app.util import utcnow

    grouped = {
        (stage, status): count
        for stage, status, count in db.query(Job.stage, Job.status, func.count(func.distinct(Job.entity_id)))
        .filter(Job.project_id == project.id)
        .group_by(Job.stage, Job.status)
        .all()
    }
    storyboard_total = db.query(func.count(Shot.id)).filter(Shot.project_id == project.id).scalar() or 0
    video_total = storyboard_total
    cast_total = (
        db.query(func.count(func.distinct(Job.entity_id)))
        .filter(Job.project_id == project.id, Job.stage.in_(["character_bible", "location_bible"]))
        .scalar()
        or 0
    )
    stored = ((project.settings or {}).get("progress") or None) if project.settings else None
    if stored is not None and not isinstance(stored, dict):
        stored = None

    def done(stage: str) -> int:
        return int(grouped.get((stage, "succeeded"), 0))

    ingest_done = done("ingest") > 0
    director_done = done("director") > 0
    script_done = done("screenwrite") > 0
    finish_done = project.status == "completed" or done("qc_final") > 0
    percent = overall_percent(
        status=project.status,
        stored=stored,
        ingest_done=ingest_done,
        director_done=director_done,
        script_done=script_done,
        cast_done=done("character_bible") + done("location_bible"),
        cast_total=cast_total,
        storyboard_done=done("storyboard"),
        storyboard_total=storyboard_total,
        video_done=done("video"),
        video_total=video_total,
        finish_done=finish_done,
    )
    running = (
        db.query(Job)
        .filter(Job.project_id == project.id, Job.status == "running")
        .order_by(Job.updated_at.desc())
        .first()
    )
    if project.status == "trial_done":
        label = "试做完成"
    elif project.status == "interrupted":
        label = "已中断"
    elif project.status == "failed":
        label = "出错"
    elif stored and stored.get("stage") == "map" and not ingest_done:
        label = stored.get("label") or "分析章节"
    elif running is not None and running.stage == "storyboard" and storyboard_total:
        label = f"镜头 {done('storyboard')}/{storyboard_total} 出图"
    elif running is not None and running.stage == "video" and video_total:
        label = f"镜头 {done('video')}/{video_total} 出视频"
    elif running is not None and running.stage in {"screenwrite", "episode_script"}:
        label = stored.get("label") if stored else "写剧本"
    elif stored and stored.get("label") and not ingest_done:
        label = stored["label"]
    elif project.status == "completed":
        label = "已成片"
    elif running is not None:
        from app.api.catalog import STAGE_LABELS

        label = STAGE_LABELS.get(running.stage, "制作中")
    else:
        label = (stored or {}).get("label") or "制作中"
    last = (
        db.query(ModelCall)
        .filter(ModelCall.project_id == project.id)
        .order_by(ModelCall.created_at.desc())
        .first()
    )
    last_call = None
    latencies: list[int] = []
    if last is not None:
        last_call = {
            "provider": last.provider,
            "model": last.model,
            "category": last.category,
            "status": last.status,
            "at": last.created_at.isoformat() if last.created_at else None,
        }
        recent = (
            db.query(ModelCall)
            .filter(ModelCall.project_id == project.id, ModelCall.latency_ms > 0)
            .order_by(ModelCall.created_at.desc())
            .limit(8)
            .all()
        )
        latencies = [call.latency_ms for call in recent]
    eta = None
    if stored and stored.get("stage") == "map" and not ingest_done:
        workers = int((project.settings or {}).get("chapter_concurrency") or get_settings().chapter_concurrency or 6)
        eta = eta_seconds(int(stored.get("done") or 0), int(stored.get("total") or 0), latencies, workers)
    stall = int((project.settings or {}).get("stall_seconds") or get_settings().stall_seconds or 180)
    view = progress_view(
        status=project.status,
        error=project.error,
        stored=stored,
        now=utcnow(),
        last_call=last_call,
        stall_seconds=stall,
        jobs_exist=bool(grouped),
        label=label,
        percent=percent,
        eta=eta,
    )
    view["episodes"] = []
    return view


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
