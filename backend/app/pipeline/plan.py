"""Episode count, chapter range, and the trial checkpoint.

The outline never creates one episode per chunk. Source chapters in the
chosen range are grouped into the chosen episode count.
"""

from app.db.models import ChapterCache, Episode, Job, ModelCall, Shot
from app.novel.chapters import group_chapters, resolved_plan, slice_chapters, split_chapters, suggest_episode_count
from app.pipeline.names import ASSEMBLE_FINAL, BIBLE_REFINE, QC_FINAL


def load_chapters(novel) -> list[str]:
    if novel is None or not (novel.text or "").strip():
        return []
    return split_chapters(novel.text)


def scoped_chapters(novel, settings: dict) -> list[str]:
    chapters = load_chapters(novel)
    return slice_chapters(chapters, settings.get("chapter_from"), settings.get("chapter_to"))


def describe(novel, settings: dict) -> dict:
    raw = load_chapters(novel)
    chosen = scoped_chapters(novel, settings)
    plan = resolved_plan(len(chosen), settings)
    groups = group_chapters(len(chosen), plan["episodes"]) if chosen else []
    return {
        "chapter_count": len(raw),
        "range_count": len(chosen),
        "suggested": suggest_episode_count(len(chosen) or 1),
        "episodes": plan["episodes"],
        "trial": plan["trial"],
        "groups": [{"episode": index + 1, "from": start + 1, "to": end} for index, (start, end) in enumerate(groups)],
        "full_read": bool(settings.get("full_read")),
        "episode_mode": settings.get("episode_mode") or "fixed",
        "chapter_from": int(settings.get("chapter_from") or 1),
        "chapter_to": int(settings.get("chapter_to") or len(raw) or 1),
    }


def trial_pending(db, project) -> bool:
    settings = project.settings or {}
    if settings.get("finish_all"):
        return False
    outlined = db.query(Episode).filter(Episode.project_id == project.id, Episode.status == "outlined").count()
    return outlined > 0


def stop_background(db, project) -> int:
    jobs = (
        db.query(Job)
        .filter(
            Job.project_id == project.id,
            Job.stage == BIBLE_REFINE,
            Job.status.in_(["queued", "running", "blocked"]),
        )
        .all()
    )
    for job in jobs:
        job.status = "interrupted"
        job.enqueued = False
        job.error = "已停止全书补读"
    settings = dict(project.settings or {})
    settings["full_read"] = False
    project.settings = settings
    return len(jobs)


def apply_plan(db, project, novel, body: dict) -> dict:
    """Save episode settings. Cached chapter notes and episodes that already have shots stay."""
    settings = dict(project.settings or {})
    if body.get("episode_mode"):
        settings["episode_mode"] = body["episode_mode"]
    if body.get("episode_count"):
        settings["episode_count"] = int(body["episode_count"])
        if body.get("episode_mode") != "auto":
            settings["episode_mode"] = "fixed"
    if body.get("produce_first"):
        settings["produce_first"] = int(body["produce_first"])
    if "chapter_from" in body and body["chapter_from"]:
        settings["chapter_from"] = int(body["chapter_from"])
    if "chapter_to" in body and body["chapter_to"]:
        settings["chapter_to"] = int(body["chapter_to"])
    if "full_read" in body:
        settings["full_read"] = bool(body["full_read"])
    chosen = scoped_chapters(novel, settings)
    plan = resolved_plan(len(chosen) or 1, settings)
    settings["episode_count"] = plan["episodes"]
    settings["produce_first"] = plan["trial"]
    project.settings = settings
    stop_background(db, project)
    project.settings = settings
    _reconcile_episodes(db, project, chosen, plan)
    db.commit()
    return describe(novel, project.settings or {})


def _reconcile_episodes(db, project, chapters: list[str], plan: dict) -> None:
    groups = group_chapters(len(chapters) or 1, plan["episodes"])
    existing = db.query(Episode).filter(Episode.project_id == project.id).all()
    by_number = {episode.number: episode for episode in existing}
    keep = set()
    for index, (start, end) in enumerate(groups, start=1):
        keep.add(index)
        title = chapters[start].splitlines()[0][:40] if chapters else f"第{index}集"
        row = by_number.get(index)
        if row is None:
            db.add(
                Episode(
                    project_id=project.id,
                    number=index,
                    title=title,
                    synopsis=f"第{start + 1}-{end}章",
                    status="outlined",
                )
            )
            continue
        has_shots = db.query(Shot).filter(Shot.episode_id == row.id).count() > 0
        if not has_shots:
            row.title = title
            row.synopsis = f"第{start + 1}-{end}章"
            row.status = "outlined"
    for episode in existing:
        if episode.number in keep:
            continue
        has_shots = db.query(Shot).filter(Shot.episode_id == episode.id).count() > 0
        if has_shots:
            continue
        db.delete(episode)
    db.flush()


def mark_trial_if_ready(db, project) -> bool:
    if not trial_pending(db, project):
        return False
    episodes = db.query(Episode).filter(Episode.project_id == project.id).all()
    produced = [episode for episode in episodes if db.query(Shot).filter(Shot.episode_id == episode.id).count()]
    if not produced or any(episode.status != "assembled" for episode in produced):
        return False
    project.status = "trial_done"
    project.phase = "trial_done"
    return True


def quote_rest(db, project) -> dict:
    actual = round(
        sum(call.cost_usd or 0 for call in db.query(ModelCall).filter(ModelCall.project_id == project.id, ModelCall.status == "succeeded").all()),
        6,
    )
    episodes = db.query(Episode).filter(Episode.project_id == project.id).all()
    done = 0
    left = 0
    for episode in episodes:
        if db.query(Shot).filter(Shot.episode_id == episode.id).count():
            done += 1
        elif episode.status == "outlined":
            left += 1
    per = (actual / done) if done else 0
    return {"actual_usd": actual, "done_episodes": done, "remaining_episodes": left, "rest_usd": round(per * left, 6)}


def cache_count(db, project_id: str) -> int:
    return db.query(ChapterCache).filter(ChapterCache.project_id == project_id).count()
