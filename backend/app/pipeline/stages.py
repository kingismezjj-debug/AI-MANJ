"""Stage handlers.

Each function consumes upstream artifacts for one entity and writes new ones.
It does not enqueue work. The executor commits, then `kick` releases whatever
the DAG now allows — including other shots that are still upstream.
"""

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.adapters.router import ModelRouter
from app.db.models import Character, Chunk, Episode, Job, Location, Novel, Project, Scene, Shot
from app.media.audio_synth import pad_wav, synth_wav
from app.media.ffmpeg_tools import burn_ass, concat_videos, duration_of, has_stream, mix_music, still_to_mp4
from app.media.fonts import find_cjk_font
from app.media.images import STYLE_SUFFIX, fit_png
from app.media.subtitles import build_ass
from app.novel.chapters import llm_segments, split_chapters
from app.pipeline.artifacts import latest_artifact, materialize, save_bytes, temp_workspace
from app.pipeline.agents import AGENT_FOR_STAGE, record
from app.pipeline.graph import build_graph, ensure_screen_job, schedule_video_regen
from app.pipeline.names import (
    ANIMATIC,
    ASSEMBLE_EPISODE,
    ASSEMBLE_FINAL,
    CHARACTER_BIBLE,
    CHARACTER_LOCK,
    BIBLE_REFINE,
    DIRECTOR,
    EPISODE_SCRIPT,
    INGEST,
    LIPSYNC,
    SCRIPT_LOCK,
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
from app.pipeline.prompts import ANALYSIS_SYSTEM, screen_system
from app.pipeline.schema import clamp_screenplay, complete_json, extract_json, parse_screenplay
from app.pipeline.visual import apply_shot_prompt, as_dict, normalize_anchors
from app.util import stable_seed


class HoldForApproval(Exception):
    """A human checkpoint is on. `kind` is `script` or `bible`."""

    def __init__(self, message: str, kind: str = "bible"):
        super().__init__(message)
        self.kind = kind


class QCRetry(Exception):
    """QC failed and a regeneration chain was queued. The project stays alive."""


def mark_shot(shot: Shot, stage: str, status: str) -> None:
    current = dict(shot.stage_status or {})
    current[stage] = status
    shot.stage_status = current


def run_stage(db: Session, job: Job) -> dict:
    project = db.get(Project, job.project_id)
    if project is None:
        raise RuntimeError("project missing")
    project.phase = job.stage
    agent = AGENT_FOR_STAGE.get(job.stage, "director")
    handler = HANDLERS[job.stage]
    if job.entity_type == "shot":
        shot = db.get(Shot, job.entity_id)
        if shot is not None:
            mark_shot(shot, job.stage, "running")
    result = handler(db, job, project)
    if job.stage != "director":
        from app.pipeline.agents import AGENT_LABELS

        record(
            db,
            project.id,
            agent,
            f"{AGENT_LABELS.get(agent, agent)}完成 {job.stage}",
            kind="result",
            status="succeeded",
            job_id=job.id,
        )
    return result


def _router(db, project, job) -> ModelRouter:
    return ModelRouter(db, project, job.id)


def _store():
    from app.storage import get_store

    return get_store()


def _chapter_workers(project: Project) -> int:
    from app.config import get_settings

    settings = get_settings()
    configured = int((project.settings or {}).get("chapter_concurrency") or settings.chapter_concurrency or 6)
    if (settings.database_url or "").startswith("sqlite"):
        return 1
    return max(1, min(configured, 16))


def _pace(prefix: str, done: int, total: int, started) -> str:
    from app.util import utcnow

    minutes = max((utcnow() - started).total_seconds() / 60, 1 / 60)
    rate = done / minutes
    return f"{prefix}分析章节 {done}/{total} · {rate:.0f} 次/分钟"


def _map_notes(db: Session, job: Job, project: Project, segments: list[tuple[int, str]], *, prefix: str = "") -> list[dict]:
    """Map every segment. Postgres runs several calls at once; SQLite stays serial."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.db.session import SessionLocal
    from app.pipeline.longform import analysis_model, map_chapter
    from app.pipeline.progress import bump
    from app.settings_store import configured_slot
    from app.util import utcnow

    total = len(segments)
    notes: list[dict | None] = [None] * total
    workers = 1 if total == 1 else _chapter_workers(project)
    started = utcnow()
    fast_model = analysis_model(configured_slot(db, project, "llm"))
    bump(db, project, stage="map", label=_pace(prefix, 0, total, started), done=0, total=total, job=job)
    project_id = project.id
    job_id = job.id

    def one(index: int, part: str) -> tuple[int, dict]:
        session = SessionLocal()
        try:
            current = session.get(Project, project_id)
            note = map_chapter(
                ModelRouter(session, current, job_id),
                part,
                index,
                db=session,
                project_id=project_id,
                model=fast_model,
            )
            session.commit()
            return index, note
        finally:
            session.close()

    done = 0
    if workers == 1:
        fresh = db.get(Project, project_id)
        for index, (_chapter, part) in enumerate(segments):
            bump(db, fresh, stage="map", label=_pace(prefix, done, total, started), done=done, total=total, job=job)
            _index, note = one(index, part)
            notes[_index] = note
            done += 1
            fresh = db.get(Project, project_id)
            job.heartbeat_at = utcnow()
            bump(db, fresh, stage="map", label=_pace(prefix, done, total, started), done=done, total=total, job=job)
            _maybe_release_early(db, fresh, job, segments, notes)
        return [note or {} for note in notes]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, index, part) for index, (_chapter, part) in enumerate(segments)]
        for future in as_completed(futures):
            index, note = future.result()
            notes[index] = note
            done += 1
            fresh = db.get(Project, project_id)
            job.heartbeat_at = utcnow()
            bump(db, fresh, stage="map", label=_pace(prefix, done, total, started), done=done, total=total, job=job)
            _maybe_release_early(db, fresh, job, segments, notes)
    return [note or {} for note in notes]


def _maybe_release_early(db: Session, project: Project, job: Job, segments: list[tuple[int, str]], notes: list[dict | None]) -> None:
    """Script the first episodes once their chapters are mapped. Later chapters keep going."""
    from app.pipeline.graph import build_graph, create_job
    from app.pipeline.longform import script_chapter
    from app.pipeline.schema import Screenplay

    settings = dict(project.settings or {})
    if settings.get("early_started"):
        return
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    chapters = split_chapters(novel.text)
    if len(chapters) < 2:
        return
    from app.novel.chapters import group_chapters, resolved_plan, slice_chapters

    chosen = slice_chapters(chapters, settings.get("chapter_from"), settings.get("chapter_to"))
    if len(chosen) < 2:
        return
    plan = resolved_plan(len(chosen), settings)
    groups = group_chapters(len(chosen), plan["episodes"])
    trial = plan["trial"]
    covered = groups[trial - 1][1] if groups else 0
    needed = {index for index, _part in segments if index < covered}
    if not needed or any(notes[pos] is None for pos, (chapter, _part) in enumerate(segments) if chapter in needed):
        return
    shots_per = int(settings.get("shots_per_episode") or settings.get("max_shots_per_episode") or 6)
    partial = [note for note in notes if note]
    bible = {"characters": [], "world": "连载", "timeline": [str(note.get("notes") or "")[:40] for note in partial[:8]], "phases": []}
    router = _router(db, project, job)
    scripted = []
    characters = []
    locations = []
    for number, (start, end) in enumerate(groups[:trial], start=1):
        source = "\n\n".join(chosen[start:end])[:24000]
        piece = script_chapter(router, project, source, bible, number, shots_per)
        scripted.extend(piece.episodes)
        if not characters:
            characters = piece.characters
            locations = piece.locations
    screenplay = Screenplay(
        title=project.title,
        characters=characters,
        locations=locations,
        episodes=scripted,
    )
    _persist_screenplay(db, project, screenplay)
    for number, (start, end) in enumerate(groups[trial:], start=trial + 1):
        title = chosen[start].splitlines()[0][:40] if chosen else f"第{number}集"
        db.add(
            Episode(
                project_id=project.id,
                number=number,
                title=title,
                synopsis=f"第{start + 1}-{end}章",
                status="outlined",
            )
        )
    settings["episode_count"] = plan["episodes"]
    settings["produce_first"] = plan["trial"]
    db.flush()
    early = create_job(
        db,
        project_id=project.id,
        stage="early_script",
        entity_type="project",
        entity_id=project.id,
        depends_on=[],
        key=f"early_script:{project.id}",
    )
    early.status = "succeeded"
    build_graph(db, project.id, early.id)
    from app.pipeline.graph import kick

    settings["early_started"] = True
    project.settings = settings
    db.commit()
    kick(project.id)


def run_ingest(db: Session, job: Job, project: Project) -> dict:
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    if not (novel.text or "").strip():
        raise RuntimeError("novel text is empty")
    from app.config import get_settings
    from app.pipeline.progress import bump

    size = int((project.settings or {}).get("chapter_chars") or get_settings().chapter_chars or 24000)
    db.query(Chunk).filter(Chunk.novel_id == novel.id).delete()
    from app.novel.chapters import slice_chapters as _slice

    scoped = _slice(split_chapters(novel.text), (project.settings or {}).get("chapter_from"), (project.settings or {}).get("chapter_to"))
    segments = llm_segments(novel.text, size=size, chapters=scoped or None)
    for index, (_chapter, piece) in enumerate(segments):
        db.add(Chunk(novel_id=novel.id, idx=index, text=piece))
    db.flush()
    router = _router(db, project, job)
    if len(segments) == 1:
        bump(db, project, stage="map", label="分析章节 1/1", done=0, total=1)
        project = db.get(Project, project.id)
        novel = db.query(Novel).filter(Novel.project_id == project.id).one()
        from app.pipeline.longform import bible_from_analysis

        payload = complete_json(router, [
            {"role": "system", "content": ANALYSIS_SYSTEM},
            {"role": "user", "content": f"<<<NOVEL\n{segments[0][1]}\n>>>"},
        ], temperature=0.3, what="小说分析")
        payload["bible"] = bible_from_analysis(payload)
        novel.analysis = payload
        bump(db, project, stage="reduce", label="生成故事圣经", done=1, total=1)
    else:
        from app.novel.chapters import skim_chapters
        from app.pipeline.graph import create_job
        from app.pipeline.longform import bible_from_analysis, reduce_bible
        from app.pipeline.names import BIBLE_REFINE

        chapters = split_chapters(novel.text)
        first = max(1, int((project.settings or {}).get("produce_first") or 2))
        fast = (project.settings or {}).get("fast_start", True) and len(chapters) > first
        if fast:
            skim = skim_chapters(chapters)
            novel.analysis = {
                "title": project.title,
                "summary": skim[0]["head"][:120] if skim else "",
                "mode": "fast",
                "skim": skim,
                "bible": {
                    "characters": [],
                    "world": "连载",
                    "timeline": [item["title"] for item in skim[:12]],
                    "phases": [],
                },
            }
            db.flush()
            from app.novel.chapters import group_chapters, resolved_plan, slice_chapters

            chosen = slice_chapters(chapters, (project.settings or {}).get("chapter_from"), (project.settings or {}).get("chapter_to"))
            plan = resolved_plan(len(chosen) or 1, project.settings or {})
            groups = group_chapters(len(chosen) or 1, plan["episodes"])
            covered = groups[plan["trial"] - 1][1] if groups else first
            head = [segment for segment in segments if segment[0] < covered]
            notes = _map_notes(db, job, project, head, prefix="快速开拍 · ")
            project = db.get(Project, project.id)
            _maybe_release_early(db, project, job, head, notes)
            if (project.settings or {}).get("full_read"):
                refine = create_job(
                    db,
                    project_id=project.id,
                    stage=BIBLE_REFINE,
                    entity_type="project",
                    entity_id=project.id,
                    depends_on=[job.id],
                    key=f"bible_refine:{project.id}",
                )
                if refine.status == "blocked":
                    refine.depends_on = [job.id]
        else:
            notes = _map_notes(db, job, project, segments)
            project = db.get(Project, project.id)
            novel = db.query(Novel).filter(Novel.project_id == project.id).one()
            bump(db, project, stage="reduce", label="生成故事圣经", done=len(segments), total=len(segments), job=job)
            project = db.get(Project, project.id)
            novel.analysis = reduce_bible(_router(db, project, job), notes)
            novel.analysis.setdefault("bible", bible_from_analysis(novel.analysis))
            novel.analysis.setdefault("title", project.title)
            novel.analysis.setdefault("summary", (novel.analysis.get("bible") or {}).get("world") or "")
    from app.pipeline.graph import ensure_director_job

    project = db.get(Project, project.id)
    ensure_director_job(db, project.id, job.id)
    return {"chunks": len(segments), "title": (novel.analysis or {}).get("title")}


def _novel_for_writer(novel: Novel, db: Session) -> str:
    return novel.text


def _complete_screenplay(router: ModelRouter, project: Project, novel_text: str, analysis: dict):
    messages = [
        {"role": "system", "content": screen_system(project.settings or {})},
        {
            "role": "user",
            "content": f"<<<NOVEL\n{novel_text}\n>>>\n分析：{json.dumps(analysis or {}, ensure_ascii=False)}",
        },
    ]
    from app.pipeline.schema import Screenplay

    return complete_json(router, messages, validate=Screenplay.model_validate, temperature=0.7, what="剧本")


def _persist_screenplay(db: Session, project: Project, screenplay) -> dict:
    db.query(Shot).filter(Shot.project_id == project.id).delete()
    db.query(Scene).filter(Scene.project_id == project.id).delete()
    db.query(Episode).filter(Episode.project_id == project.id).delete()
    db.query(Character).filter(Character.project_id == project.id).delete()
    db.query(Location).filter(Location.project_id == project.id).delete()
    db.flush()
    for spec in screenplay.characters:
        appearance = spec.appearance or spec.description or spec.name
        db.add(
            Character(
                project_id=project.id,
                name=spec.name,
                description=spec.description or appearance,
                appearance=appearance,
                personality=spec.personality,
                voice_hint=spec.voice_hint,
                anchors=normalize_anchors(appearance, getattr(spec, "anchors", None)),
                seed=stable_seed(project.id, spec.name),
                status="pending",
                locked=False,
            )
        )
    for spec in screenplay.locations:
        db.add(
            Location(
                project_id=project.id,
                name=spec.name,
                description=spec.description,
                status="pending",
            )
        )
    shot_count = 0
    for episode_spec in screenplay.episodes:
        episode = Episode(
            project_id=project.id,
            number=episode_spec.number,
            title=episode_spec.title,
            hook=episode_spec.hook,
            cliffhanger=episode_spec.cliffhanger,
            synopsis=episode_spec.synopsis,
            status="pending",
        )
        db.add(episode)
        db.flush()
        running_number = 1
        for scene_spec in episode_spec.scenes:
            scene = Scene(
                project_id=project.id,
                episode_id=episode.id,
                number=scene_spec.number,
                location_name=scene_spec.location,
                description=scene_spec.description,
            )
            db.add(scene)
            db.flush()
            for shot_spec in scene_spec.shots:
                names = list(shot_spec.characters or [])
                if shot_spec.speaker and shot_spec.speaker not in names:
                    names.append(shot_spec.speaker)
                db.add(
                    Shot(
                        project_id=project.id,
                        episode_id=episode.id,
                        scene_id=scene.id,
                        number=running_number,
                        description=shot_spec.description,
                        dialogue=shot_spec.dialogue,
                        speaker=shot_spec.speaker,
                        camera=shot_spec.camera,
                        duration_sec=shot_spec.duration_sec,
                        visual_prompt=shot_spec.visual_prompt,
                        character_names=names,
                        stage_status={},
                        seed=stable_seed(project.id, str(running_number), shot_spec.description[:40]),
                        quality="draft" if (project.settings or {}).get("draft_mode") else "final",
                    )
                )
                running_number += 1
                shot_count += 1
    db.flush()
    for shot in db.query(Shot).filter(Shot.project_id == project.id).all():
        apply_shot_prompt(db, shot)
    return {"episodes": len(screenplay.episodes), "shots": shot_count, "title": screenplay.title}


def run_bible_refine(db: Session, job: Job, project: Project) -> dict:
    """Finish the chapters the fast open did not read. Off during a trial unless full_read is on."""
    from app.pipeline.longform import bible_from_analysis, reduce_bible
    from app.pipeline.progress import bump

    if not (project.settings or {}).get("full_read"):
        return {"skipped": True}
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    size = int((project.settings or {}).get("chapter_chars") or 24000)
    from app.novel.chapters import slice_chapters as _slice

    scoped = _slice(split_chapters(novel.text), (project.settings or {}).get("chapter_from"), (project.settings or {}).get("chapter_to"))
    segments = llm_segments(novel.text, size=size, chapters=scoped or None)
    first = max(1, int((project.settings or {}).get("produce_first") or 2))
    tail = [segment for segment in segments if segment[0] >= first] or segments
    notes = _map_notes(db, job, project, tail, prefix="后台补全 · ")
    project = db.get(Project, project.id)
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    bump(db, project, stage="reduce", label="后台生成全书圣经", done=len(tail), total=len(tail), job=job)
    project = db.get(Project, project.id)
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    reduced = reduce_bible(_router(db, project, job), notes)
    merged = dict(novel.analysis or {})
    merged.update({key: value for key, value in reduced.items() if key != "bible"})
    merged["bible"] = reduced.get("bible") or bible_from_analysis(reduced)
    merged["mode"] = "full"
    novel.analysis = merged
    return {"chapters": len(tail)}


def run_director(db: Session, job: Job, project: Project) -> dict:
    from app.pipeline.progress import bump

    bump(db, project, stage="director", label="总控在读小说", done=0, total=1)
    project = db.get(Project, project.id)
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    if len(novel.text) <= 8000:
        source = novel.text
    else:
        chapters = split_chapters(novel.text)
        bible = (novel.analysis or {}).get("bible") or {}
        source = json.dumps(bible, ensure_ascii=False) + "\n" + chapters[-1][:2000]
    plan = complete_json(
        _router(db, project, job),
        [
            {
                "role": "system",
                "content": "TASK:DIRECTOR\n你是漫剧总控。只输出 json。示例：{\"summary\":\"\",\"episodes\":1,\"shot_budget\":4,\"dispatch\":[\"编剧\"]}",
            },
            {"role": "user", "content": f"<<<NOVEL\n{source}\n>>>"},
        ],
        temperature=0.4,
        what="总控计划",
    )
    settings = dict(project.settings or {})
    settings["director_plan"] = plan
    project.settings = settings
    dispatch = plan.get("dispatch") or ["编剧", "定妆", "分镜", "视频", "配音", "质检"]
    record(
        db,
        project.id,
        "director",
        f"总控读完小说：{plan.get('summary') or project.title}。计划 {plan.get('episodes', 1)} 集、约 {plan.get('shot_budget', 4)} 个镜头。",
        kind="plan",
        status="succeeded",
        job_id=job.id,
    )
    for name in dispatch:
        record(db, project.id, "director", f"派给{name}", kind="dispatch", status="queued", job_id=job.id)
    ensure_screen_job(db, project.id, job.id)
    return {"dispatch": dispatch, "shot_budget": plan.get("shot_budget")}


def run_screenwrite(db: Session, job: Job, project: Project) -> dict:
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    if len(split_chapters(novel.text)) > 1:
        return _write_series(db, job, project, novel)
    screenplay = _complete_screenplay(_router(db, project, job), project, _novel_for_writer(novel, db), novel.analysis or {})
    screenplay = clamp_screenplay(screenplay, project.settings or {})
    shot_total = sum(len(scene.shots) for episode in screenplay.episodes for scene in episode.scenes)
    if shot_total == 0:
        raise RuntimeError("screenplay has no shots")
    summary = _persist_screenplay(db, project, screenplay)
    payload = json.dumps(screenplay.model_dump(), ensure_ascii=False).encode("utf-8")
    save_bytes(
        db,
        _store(),
        project_id=project.id,
        entity_type="project",
        entity_id=project.id,
        kind="screenplay",
        data=payload,
        content_type="application/json",
        job_id=job.id,
        meta=summary,
    )
    build_graph(db, project.id, job.id)
    from app.pipeline.assets import bind_project_cards

    bind_project_cards(db, project.id)
    return summary


def _write_series(db: Session, job: Job, project: Project, novel: Novel) -> dict:
    from app.pipeline.assets import bind_project_cards
    from app.pipeline.longform import build_outline, queue_remaining_scripts, script_chapter
    from app.pipeline.schema import Screenplay

    chapters = split_chapters(novel.text)
    settings = project.settings or {}
    count = int(settings.get("episode_count") or 60)
    first = int(settings.get("produce_first") or 2)
    shots_per = int(settings.get("shots_per_episode") or settings.get("max_shots_per_episode") or 6)
    from app.pipeline.progress import bump

    if db.query(Shot).filter(Shot.project_id == project.id).count():
        bump(db, project, stage="script", label="补写后面的集", done=first, total=max(first, len(chapters)))
        project = db.get(Project, project.id)
        novel = db.query(Novel).filter(Novel.project_id == project.id).one()
        bible = (novel.analysis or {}).get("bible") or {}
        outline = build_outline(_router(db, project, job), bible, chapters, count, shots_per)
        existing = {episode.number for episode in db.query(Episode).filter(Episode.project_id == project.id).all()}
        for item in outline:
            number = int(item["number"])
            if number in existing:
                continue
            db.add(
                Episode(
                    project_id=project.id,
                    number=number,
                    title=item.get("title") or f"第{number}集",
                    synopsis=item.get("synopsis") or "",
                    status="outlined",
                )
            )
        db.flush()
        queue_remaining_scripts(db, project.id, job.id)
        bind_project_cards(db, project.id)
        return {"episodes": len(existing), "outlined": len(outline), "early": True}
    bible = (novel.analysis or {}).get("bible") or {}
    router = _router(db, project, job)
    bump(db, project, stage="script", label=f"写前 {first} 集剧本", done=0, total=max(first, 1))
    outline = build_outline(router, bible, chapters, count, shots_per)
    if not outline:
        outline = [
            {"number": index + 1, "title": f"第{index + 1}集", "synopsis": chapter[:80], "chapter_index": index}
            for index, chapter in enumerate(chapters[:count])
        ]
    first = max(1, min(first, len(outline)))
    scripted = []
    characters = []
    locations = []
    for item in outline[:first]:
        index = int(item.get("chapter_index") or (int(item["number"]) - 1))
        index = max(0, min(index, len(chapters) - 1))
        piece = script_chapter(router, project, chapters[index], bible, int(item["number"]), shots_per)
        scripted.extend(piece.episodes)
        if not characters:
            characters = piece.characters
            locations = piece.locations
    screenplay = Screenplay(
        title=(novel.analysis or {}).get("title") or project.title,
        characters=characters,
        locations=locations,
        episodes=scripted,
    )
    summary = _persist_screenplay(db, project, screenplay)
    for item in outline[first:]:
        db.add(
            Episode(
                project_id=project.id,
                number=int(item["number"]),
                title=item.get("title") or f"第{item['number']}集",
                synopsis=item.get("synopsis") or "",
                status="outlined",
            )
        )
    db.flush()
    build_graph(db, project.id, job.id)
    queue_remaining_scripts(db, project.id, job.id)
    bind_project_cards(db, project.id)
    summary["outlined"] = len(outline) - first
    return summary


def run_episode_script(db: Session, job: Job, project: Project) -> dict:
    from app.pipeline.assets import bind_project_cards
    from app.pipeline.longform import append_episode, extend_graph, script_chapter

    episode = db.get(Episode, job.entity_id)
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    chapters = split_chapters(novel.text)
    index = max(0, min(episode.number - 1, len(chapters) - 1))
    shots_per = int((project.settings or {}).get("shots_per_episode") or 6)
    bible = (novel.analysis or {}).get("bible") or {}
    screenplay = script_chapter(_router(db, project, job), project, chapters[index], bible, episode.number, shots_per)
    append_episode(db, project, screenplay)
    extend_graph(db, project.id, episode.id)
    bind_project_cards(db, project.id)
    return {"episode": episode.number}


def run_script_lock(db: Session, job: Job, project: Project) -> dict:
    settings = project.settings or {}
    if settings.get("require_script_approval") and not settings.get("script_approved"):
        raise HoldForApproval("waiting for script approval", kind="script")
    return {"approved": True}


def run_character_bible(db: Session, job: Job, project: Project) -> dict:
    character = db.get(Character, job.entity_id)
    if character is None:
        raise RuntimeError("character missing")
    anchors = as_dict(character.anchors)
    prompt = (
        "角色定妆四宫格：脸部特写、发型、服装、色板。"
        f"脸：{anchors.get('face') or character.appearance}。"
        f"发型：{anchors.get('hair', '')}。"
        f"服装：{anchors.get('outfit', '')}。"
        f"色板：{anchors.get('palette', '')}。"
        f"纯色背景。{STYLE_SUFFIX}"
    )
    result = _router(db, project, job).image(
        prompt=prompt,
        width=project.width,
        height=project.height,
        seed=character.seed,
        reference_images=[],
    )
    png = fit_png(result.data, project.width, project.height)
    artifact = save_bytes(
        db,
        _store(),
        project_id=project.id,
        entity_type="character",
        entity_id=character.id,
        kind="character_ref",
        data=png,
        content_type="image/png",
        job_id=job.id,
        meta={"seed": character.seed, "provider": result.provider, "reference_count": 0},
    )
    character.reference_key = artifact.storage_key
    if (project.settings or {}).get("require_bible_approval"):
        character.status = "generated"
        character.locked = False
    else:
        character.status = "approved"
        character.locked = True
    return {"artifact_id": artifact.id, "locked": character.locked}


def run_location_bible(db: Session, job: Job, project: Project) -> dict:
    location = db.get(Location, job.entity_id)
    if location is None:
        raise RuntimeError("location missing")
    prompt = f"场景概念图，无人物，{location.name}，{location.description}。{STYLE_SUFFIX}"
    seed = stable_seed(project.id, location.name)
    result = _router(db, project, job).image(
        prompt=prompt,
        width=project.width,
        height=project.height,
        seed=seed,
    )
    png = fit_png(result.data, project.width, project.height)
    artifact = save_bytes(
        db,
        _store(),
        project_id=project.id,
        entity_type="location",
        entity_id=location.id,
        kind="location_ref",
        data=png,
        content_type="image/png",
        job_id=job.id,
        meta={"seed": seed, "provider": result.provider},
    )
    location.reference_key = artifact.storage_key
    location.status = "ready"
    return {"artifact_id": artifact.id}


def run_character_lock(db: Session, job: Job, project: Project) -> dict:
    characters = db.query(Character).filter(Character.project_id == project.id).all()
    if characters and not all(character.locked for character in characters):
        raise HoldForApproval("waiting for character approval", kind="bible")
    return {"locked": len(characters)}


def _refs_for_shot(db: Session, store, shot: Shot) -> list[bytes]:
    refs: list[bytes] = []
    names = list(shot.character_names or [])
    if shot.speaker and shot.speaker not in names:
        names.append(shot.speaker)
    for name in names:
        character = (
            db.query(Character).filter(Character.project_id == shot.project_id, Character.name == name).one_or_none()
        )
        if character and character.reference_key:
            refs.append(store.get(character.reference_key))
    return refs


def run_storyboard(db: Session, job: Job, project: Project) -> dict:
    shot = db.get(Shot, job.entity_id)
    store = _store()
    refs = _refs_for_shot(db, store, shot)
    scene = db.get(Scene, shot.scene_id)
    location_note = ""
    if scene is not None:
        location = (
            db.query(Location)
            .filter(Location.project_id == project.id, Location.name == scene.location_name)
            .one_or_none()
        )
        if location and location.reference_key:
            refs.append(store.get(location.reference_key))
            location_note = f"场景：{location.name}，{location.description}。"
    apply_shot_prompt(db, shot)
    prompt = f"{shot.video_prompt}\n{location_note}{STYLE_SUFFIX}"
    result = _router(db, project, job).image(
        prompt=prompt,
        width=project.width,
        height=project.height,
        seed=shot.seed,
        reference_images=refs,
    )
    png = fit_png(result.data, project.width, project.height)
    artifact = save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="keyframe",
        data=png,
        content_type="image/png",
        job_id=job.id,
        meta={"reference_count": len(refs), "provider": result.provider, "seed": shot.seed, "anchors": True},
    )
    mark_shot(shot, STORYBOARD, "succeeded")
    return {"artifact_id": artifact.id, "reference_count": len(refs)}


def _voice_hint(db: Session, shot: Shot) -> str:
    if not shot.speaker:
        return "female"
    character = db.query(Character).filter(Character.project_id == shot.project_id, Character.name == shot.speaker).one_or_none()
    return (character.voice_hint if character else "") or "female"


def _speech_bytes(db, project, job, shot: Shot, speed: float) -> tuple[bytes, str, dict]:
    text = (shot.dialogue or "").strip()
    if not text:
        return synth_wav(shot.duration_sec, freq=180, volume=0.08), "audio/wav", {"silent": True}
    result = _router(db, project, job).tts(text=text, voice=_voice_hint(db, shot), speed=speed)
    timing = as_dict(shot.dialogue_timing) or {"start_sec": 0.2, "end_sec": shot.duration_sec}
    data = result.data
    mime = result.mime
    if mime == "audio/wav":
        data = pad_wav(data, shot.duration_sec, float(timing.get("start_sec") or 0))
    return data, mime, {"provider": result.provider, "chars": len(text), "dialogue_timing": timing}


def run_tts_temp(db: Session, job: Job, project: Project) -> dict:
    shot = db.get(Shot, job.entity_id)
    data, mime, meta = _speech_bytes(db, project, job, shot, speed=1.05)
    artifact = save_bytes(
        db,
        _store(),
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="tts_temp",
        data=data,
        content_type=mime,
        job_id=job.id,
        meta=meta,
    )
    mark_shot(shot, TTS_TEMP, "succeeded")
    return {"artifact_id": artifact.id}


def run_voice(db: Session, job: Job, project: Project) -> dict:
    shot = db.get(Shot, job.entity_id)
    data, mime, meta = _speech_bytes(db, project, job, shot, speed=1.0)
    artifact = save_bytes(
        db,
        _store(),
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="voice",
        data=data,
        content_type=mime,
        job_id=job.id,
        meta=meta,
    )
    mark_shot(shot, VOICE, "succeeded")
    return {"artifact_id": artifact.id}


def run_video(db: Session, job: Job, project: Project) -> dict:
    settings = project.settings or {}
    if settings.get("require_grid_confirm") and not settings.get("grid_confirmed"):
        raise HoldForApproval("先确认分镜格", kind="grid")
    shot = db.get(Shot, job.entity_id)
    store = _store()
    frame = latest_artifact(db, shot.id, "keyframe")
    if frame is None:
        raise RuntimeError("keyframe missing")
    refs = _refs_for_shot(db, store, shot)
    apply_shot_prompt(db, shot)
    prompt = shot.video_prompt or shot.visual_prompt or shot.description
    duration = shot.duration_sec
    width, height = project.width, project.height
    if settings.get("draft_mode") and (shot.quality or "final") != "final":
        width, height = 480, 854
    if settings.get("merge_shots"):
        from app.pipeline.longform import lead_of, timestamped_prompt

        grouped = lead_of(db, shot)
        if grouped is not None:
            head, group = grouped
            if head.id != shot.id:
                shared = latest_artifact(db, head.id, "video")
                if shared is not None:
                    data = store.get(shared.storage_key)
                    artifact = save_bytes(
                        db,
                        store,
                        project_id=project.id,
                        entity_type="shot",
                        entity_id=shot.id,
                        kind="video",
                        data=data,
                        content_type="video/mp4",
                        job_id=job.id,
                        meta={"merged_into": head.id, "provider": "merged"},
                    )
                    mark_shot(shot, VIDEO, "succeeded")
                    return {"artifact_id": artifact.id, "merged_into": head.id}
            prompt = timestamped_prompt(group)
            duration = sum(float(item.duration_sec or 2.5) for item in group)
    result = _router(db, project, job).video(
        prompt=prompt,
        image=store.get(frame.storage_key),
        reference_images=refs,
        duration_sec=duration,
        seed=shot.seed,
        aspect_ratio=project.aspect_ratio,
        width=width,
        height=height,
        last_image=None,
    )
    if result.duration_sec and abs(float(result.duration_sec) - float(shot.duration_sec or 0)) > 0.2:
        shot.duration_sec = float(result.duration_sec)
    artifact = save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="video",
        data=result.data,
        content_type="video/mp4",
        job_id=job.id,
        meta={
            "provider": result.provider,
            "reference_count": len(refs),
            "duration_sec": result.duration_sec,
            "dialogue_timing": shot.dialogue_timing,
            "notice": (result.extra or {}).get("notice") or "",
        },
    )
    mark_shot(shot, VIDEO, "succeeded")
    return {"artifact_id": artifact.id}


def run_animatic(db: Session, job: Job, project: Project) -> dict:
    shot = db.get(Shot, job.entity_id)
    store = _store()
    frame = latest_artifact(db, shot.id, "keyframe")
    audio = latest_artifact(db, shot.id, "tts_temp")
    if frame is None or audio is None:
        raise RuntimeError("animatic inputs missing")
    with temp_workspace() as folder:
        root = Path(folder)
        image = materialize(store, frame, root, "frame.png")
        speech = materialize(store, audio, root, "temp" + Path(audio.storage_key).suffix)
        out = root / "animatic.mp4"
        still_to_mp4(str(image), str(out), shot.duration_sec, project.width, project.height, audio_path=str(speech))
        data = out.read_bytes()
    artifact = save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="animatic",
        data=data,
        content_type="video/mp4",
        job_id=job.id,
        meta={"duration_sec": shot.duration_sec},
    )
    mark_shot(shot, ANIMATIC, "succeeded")
    return {"artifact_id": artifact.id}


def run_lipsync(db: Session, job: Job, project: Project) -> dict:
    shot = db.get(Shot, job.entity_id)
    store = _store()
    video = latest_artifact(db, shot.id, "video")
    voice = latest_artifact(db, shot.id, "voice")
    if video is None or voice is None:
        raise RuntimeError("lip-sync inputs missing")
    video_bytes = store.get(video.storage_key)
    audio_bytes = store.get(voice.storage_key)
    result = _router(db, project, job).lipsync(
        video=video_bytes,
        audio=audio_bytes,
        video_url=store.presign(video.storage_key),
        audio_url=store.presign(voice.storage_key),
    )
    artifact = save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="lipsync",
        data=result.data,
        content_type="video/mp4",
        job_id=job.id,
        meta={"provider": result.provider, "duration_sec": result.duration_sec},
    )
    mark_shot(shot, LIPSYNC, "succeeded")
    return {"artifact_id": artifact.id}


def _shot_issues(db: Session, store, shot: Shot) -> list[str]:
    issues: list[str] = []
    lipsync = latest_artifact(db, shot.id, "lipsync")
    frame = latest_artifact(db, shot.id, "keyframe")
    if frame is None or frame.byte_size < 200:
        issues.append("keyframe missing")
    if lipsync is None or lipsync.byte_size < 400:
        issues.append("lipsync missing")
        return issues
    with temp_workspace() as folder:
        path = materialize(store, lipsync, Path(folder), "clip.mp4")
        try:
            if duration_of(path) < 0.4:
                issues.append("lipsync too short")
            if not has_stream(path, "video"):
                issues.append("no video stream")
            if not has_stream(path, "audio"):
                issues.append("no audio stream")
        except Exception as exc:
            issues.append(f"ffprobe failed: {exc}")
    return issues


def run_shot_qc(db: Session, job: Job, project: Project) -> dict:
    shot = db.get(Shot, job.entity_id)
    settings = dict(project.settings or {})
    remaining = int(settings.get("force_qc_fail_remaining") or 0)
    issues = _shot_issues(db, _store(), shot)
    if remaining > 0:
        settings["force_qc_fail_remaining"] = remaining - 1
        project.settings = settings
        issues.append("forced qc failure")
    if not issues:
        mark_shot(shot, SHOT_QC, "succeeded")
        return {"ok": True}
    max_retries = int(settings.get("max_qc_retries") or 1)
    if shot.qc_attempts < max_retries:
        shot.qc_attempts += 1
        schedule_video_regen(db, shot)
        mark_shot(shot, SHOT_QC, "retry")
        record(
            db,
            project.id,
            "director",
            f"质检未过镜头 {shot.number}（{'；'.join(issues)}）。总控决定只重做这一镜的视频。",
            kind="review",
            status="retry",
            job_id=job.id,
        )
        raise QCRetry("; ".join(issues))
    mark_shot(shot, SHOT_QC, "failed")
    raise RuntimeError("qc failed: " + "; ".join(issues))


def _ordered_shots(db: Session, episode_id: str) -> list[Shot]:
    return db.query(Shot).filter(Shot.episode_id == episode_id).order_by(Shot.number).all()


def run_music(db: Session, job: Job, project: Project) -> dict:
    episode = db.get(Episode, job.entity_id)
    shots = _ordered_shots(db, episode.id)
    duration = max(4.0, sum(shot.duration_sec for shot in shots))
    prompt = f"竖屏短剧配乐，克制，{episode.synopsis or episode.title}"
    result = _router(db, project, job).music(prompt=prompt, duration_sec=duration)
    artifact = save_bytes(
        db,
        _store(),
        project_id=project.id,
        entity_type="episode",
        entity_id=episode.id,
        kind="music",
        data=result.data,
        content_type=result.mime,
        job_id=job.id,
        meta={"duration_sec": duration, "provider": result.provider},
    )
    return {"artifact_id": artifact.id}


def run_assemble_episode(db: Session, job: Job, project: Project) -> dict:
    episode = db.get(Episode, job.entity_id)
    store = _store()
    shots = _ordered_shots(db, episode.id)
    with temp_workspace() as folder:
        root = Path(folder)
        clips: list[str] = []
        for shot in shots:
            lipsync = latest_artifact(db, shot.id, "lipsync")
            if lipsync is None:
                raise RuntimeError(f"shot {shot.number} has no lip-sync")
            path = materialize(store, lipsync, root, f"shot-{shot.number:02d}.mp4")
            clips.append(str(path))
        joined = root / "joined.mp4"
        concat_videos(clips, str(joined))
        music = latest_artifact(db, episode.id, "music")
        final_path = joined
        if music is not None:
            bed = materialize(store, music, root, "bed" + Path(music.storage_key).suffix)
            mixed = root / "mixed.mp4"
            mix_music(str(joined), str(bed), str(mixed))
            final_path = mixed
        data = final_path.read_bytes()
    artifact = save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="episode",
        entity_id=episode.id,
        kind="episode_cut",
        data=data,
        content_type="video/mp4",
        job_id=job.id,
        meta={"shots": len(shots)},
    )
    episode.status = "assembled"
    from app.pipeline.plan import mark_trial_if_ready

    mark_trial_if_ready(db, project)
    return {"artifact_id": artifact.id}


def _cue_text(shot: Shot) -> str:
    if (shot.dialogue or "").strip():
        prefix = f"{shot.speaker}：" if shot.speaker else ""
        return prefix + shot.dialogue.strip()
    return (shot.description or "").strip()[:28]


def run_assemble_final(db: Session, job: Job, project: Project) -> dict:
    store = _store()
    episodes = db.query(Episode).filter(Episode.project_id == project.id).order_by(Episode.number).all()
    with temp_workspace() as folder:
        root = Path(folder)
        episode_paths: list[str] = []
        cues: list[tuple[float, float, str]] = []
        cursor = 0.0
        for episode in episodes:
            cut = latest_artifact(db, episode.id, "episode_cut")
            if cut is None:
                raise RuntimeError(f"episode {episode.number} has no cut")
            path = materialize(store, cut, root, f"ep-{episode.number:02d}.mp4")
            episode_paths.append(str(path))
            for shot in _ordered_shots(db, episode.id):
                lipsync = latest_artifact(db, shot.id, "lipsync")
                if lipsync is None:
                    continue
                clip = materialize(store, lipsync, root, f"cue-{shot.id}.mp4")
                length = duration_of(clip) or shot.duration_sec
                text = _cue_text(shot)
                if text:
                    cues.append((cursor, cursor + length, text))
                cursor += length
        joined = root / "series.mp4"
        if len(episode_paths) == 1:
            series_bytes = Path(episode_paths[0]).read_bytes()
            joined.write_bytes(series_bytes)
        else:
            concat_videos(episode_paths, str(joined))
        font = find_cjk_font()
        show_label = bool((project.settings or {}).get("show_ai_label", True))
        ass = build_ass(cues, total=max(cursor, duration_of(joined)), show_label=show_label, width=project.width, height=project.height)
        ass_path = root / "captions.ass"
        ass_path.write_text(ass, encoding="utf-8")
        burned = root / "final.mp4"
        burned_ok = False
        if font is not None:
            burn_ass(str(joined), str(ass_path), str(font.parent), str(burned))
            burned_ok = True
            final_bytes = burned.read_bytes()
        else:
            final_bytes = joined.read_bytes()
        subtitle_bytes = ass.encode("utf-8")
    final = save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="project",
        entity_id=project.id,
        kind="final_video",
        data=final_bytes,
        content_type="video/mp4",
        job_id=job.id,
        meta={"burned_subtitles": burned_ok, "ai_label": show_label, "duration_sec": cursor},
    )
    save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="project",
        entity_id=project.id,
        kind="subtitle",
        data=subtitle_bytes,
        content_type="text/x-ssa",
        job_id=job.id,
        meta={"cues": len(cues)},
    )
    project.final_artifact_id = final.id
    return {"artifact_id": final.id, "burned_subtitles": burned_ok}


def run_qc_final(db: Session, job: Job, project: Project) -> dict:
    final = latest_artifact(db, project.id, "final_video")
    if final is None:
        raise RuntimeError("final video missing")
    store = _store()
    with temp_workspace() as folder:
        path = materialize(store, final, Path(folder), "final.mp4")
        duration = duration_of(path)
        if duration < 1:
            raise RuntimeError("final video is too short")
        if not has_stream(path, "video") or not has_stream(path, "audio"):
            raise RuntimeError("final video is missing audio or video")
    project.status = "completed"
    project.phase = "completed"
    project.error = None
    from app.billing import settle_project

    settlement = settle_project(db, project)
    return {"ok": True, "artifact_id": final.id, "duration_sec": duration, "settlement": settlement}


HANDLERS = {
    INGEST: run_ingest,
    BIBLE_REFINE: run_bible_refine,
    DIRECTOR: run_director,
    SCREENWRITE: run_screenwrite,
    EPISODE_SCRIPT: run_episode_script,
    SCRIPT_LOCK: run_script_lock,
    CHARACTER_BIBLE: run_character_bible,
    LOCATION_BIBLE: run_location_bible,
    CHARACTER_LOCK: run_character_lock,
    STORYBOARD: run_storyboard,
    TTS_TEMP: run_tts_temp,
    VOICE: run_voice,
    VIDEO: run_video,
    ANIMATIC: run_animatic,
    LIPSYNC: run_lipsync,
    SHOT_QC: run_shot_qc,
    MUSIC: run_music,
    ASSEMBLE_EPISODE: run_assemble_episode,
    ASSEMBLE_FINAL: run_assemble_final,
    QC_FINAL: run_qc_final,
}
