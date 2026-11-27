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
    """Save episode settings. Chapter-note cache and the trial scripts stay."""
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
    settings["full_read"] = bool(settings.get("full_read"))
    project.settings = settings
    if not settings["full_read"]:
        stop_background(db, project)
        project.settings = settings
    compact_project(db, project, chosen, plan)
    db.commit()
    return describe(novel, project.settings or {})


def compact_project(db, project, chapters: list[str] | None = None, plan: dict | None = None) -> dict:
    """Drop runaway episodes, shots, jobs, and artifacts.

    Scripts for episodes 1..trial are kept. Later episodes inside the plan
    become empty outlines. Chapter-note cache is not touched. A redelivered
    Celery message for a deleted job finds nothing and does not start a new fill.
    """
    from sqlalchemy import delete, func, select

    from app.db.models import Artifact, Scene, ShotAsset

    settings = dict(project.settings or {})
    chapters = list(chapters or [])
    if plan is None:
        planned = int(settings.get("episode_count") or 1)
        trial = min(int(settings.get("produce_first") or 2), planned)
    else:
        planned = int(plan["episodes"])
        trial = min(int(plan["trial"]), planned)
    project_id = project.id
    drop_episodes = select(Episode.id).where(Episode.project_id == project_id, Episode.number > trial)
    drop_shots = select(Shot.id).where(Shot.episode_id.in_(drop_episodes))
    db.execute(delete(ShotAsset).where(ShotAsset.shot_id.in_(drop_shots)))
    db.execute(delete(Artifact).where(Artifact.project_id == project_id, Artifact.entity_id.in_(drop_shots)))
    db.execute(
        delete(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.entity_type == "episode",
            Artifact.entity_id.in_(drop_episodes),
        )
    )
    kept_shots = select(Shot.id).where(
        Shot.episode_id.in_(select(Episode.id).where(Episode.project_id == project_id, Episode.number <= trial))
    )
    db.execute(delete(Job).where(Job.project_id == project_id, Job.entity_id.in_(drop_shots)))
    db.execute(
        delete(Job).where(Job.project_id == project_id, Job.entity_type == "shot", ~Job.entity_id.in_(kept_shots))
    )
    db.execute(
        delete(Job).where(Job.project_id == project_id, Job.entity_type == "episode", Job.entity_id.in_(drop_episodes))
    )
    db.execute(
        delete(Job).where(
            Job.project_id == project_id,
            Job.status.in_(["queued", "running", "blocked"]),
            Job.stage.in_([ASSEMBLE_FINAL, QC_FINAL]),
        )
    )
    db.execute(delete(Shot).where(Shot.episode_id.in_(drop_episodes)))
    db.execute(delete(Scene).where(Scene.episode_id.in_(drop_episodes)))
    db.execute(delete(Episode).where(Episode.project_id == project_id, Episode.number > planned))
    db.query(Episode).filter(
        Episode.project_id == project_id, Episode.number > trial, Episode.number <= planned
    ).update({Episode.status: "outlined"}, synchronize_session=False)
    if chapters:
        groups = group_chapters(max(1, len(chapters)), planned)
        existing = {
            row.number: row
            for row in db.query(Episode).filter(Episode.project_id == project_id, Episode.number <= planned).all()
        }
        for index, (start, end) in enumerate(groups, start=1):
            title = chapters[start].splitlines()[0][:40] if start < len(chapters) else f"第{index}集"
            row = existing.get(index)
            if row is None:
                db.add(
                    Episode(
                        project_id=project_id,
                        number=index,
                        title=title,
                        synopsis=f"第{start + 1}-{end}章",
                        status="outlined",
                    )
                )
            elif index > trial:
                row.title = title
                row.synopsis = f"第{start + 1}-{end}章"
                row.status = "outlined"
    else:
        have = {
            number
            for (number,) in db.query(Episode.number).filter(Episode.project_id == project_id, Episode.number <= planned)
        }
        for index in range(1, planned + 1):
            if index not in have:
                db.add(Episode(project_id=project_id, number=index, title=f"第{index}集", status="outlined"))
    settings["episode_count"] = planned
    settings["produce_first"] = trial
    project.settings = settings
    still = (
        db.query(Job.id)
        .filter(Job.project_id == project_id, Job.status.in_(["queued", "running", "blocked"]))
        .first()
    )
    if project.status == "running" and still is None:
        project.status = "trial_done"
    db.flush()
    return {
        "episodes": planned,
        "trial": trial,
        "episode_rows": db.query(func.count(Episode.id)).filter(Episode.project_id == project_id).scalar() or 0,
    }


def compact_overgrown(db) -> list[str]:
    """Collapse a project whose episode rows outgrew the saved plan. Idempotent."""
    from sqlalchemy import func, text

    from app.db.models import Project

    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(842181)"))
    counts = db.query(Episode.project_id, func.count(Episode.id)).group_by(Episode.project_id).all()
    changed = []
    for project_id, count in counts:
        project = db.get(Project, project_id)
        if project is None:
            continue
        settings = project.settings or {}
        planned = int(settings.get("episode_count") or 0)
        if planned <= 0 or count <= planned:
            continue
        stop_background(db, project)
        trial = min(int(settings.get("produce_first") or 2), planned)
        from app.db.models import Novel

        novel = db.query(Novel).filter(Novel.project_id == project.id).one_or_none()
        chosen = scoped_chapters(novel, settings)
        compact_project(db, project, chosen, {"episodes": planned, "trial": trial})
        changed.append(project_id)
        import logging

        logging.getLogger(__name__).info(
            "compacted project %s from %s episode rows to %s (trial %s)",
            project_id,
            count,
            planned,
            trial,
        )
    if changed:
        db.commit()
    return changed


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
    from sqlalchemy import func

    actual = (
        db.query(func.coalesce(func.sum(ModelCall.cost_usd), 0))
        .filter(ModelCall.project_id == project.id, ModelCall.status == "succeeded")
        .scalar()
        or 0
    )
    done = (
        db.query(func.count(func.distinct(Shot.episode_id)))
        .filter(Shot.project_id == project.id, Shot.episode_id != "")
        .scalar()
        or 0
    )
    produced = db.query(Shot.episode_id).filter(Shot.project_id == project.id, Shot.episode_id != "").distinct()
    left = (
        db.query(func.count(Episode.id))
        .filter(Episode.project_id == project.id, Episode.status == "outlined", ~Episode.id.in_(produced))
        .scalar()
        or 0
    )
    per = (actual / done) if done else 0
    return {
        "actual_usd": round(float(actual), 6),
        "done_episodes": done,
        "remaining_episodes": left,
        "rest_usd": round(per * left, 6),
    }


def cache_count(db, project_id: str) -> int:
    return db.query(ChapterCache).filter(ChapterCache.project_id == project_id).count()
