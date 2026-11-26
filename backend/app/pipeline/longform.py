"""Whole-book bible, episode outline, and episode-by-episode scripting.

Episode N+1's script job depends only on the screenplay job, not on episode N's
render, so the two can be in flight together. "先出前 N 集" leaves the rest
outlined until continue is called.
"""

import json

from app.db.models import Character, Episode, Job, Location, Novel, Scene, Shot
from app.pipeline.names import (
    ANIMATIC,
    ASSEMBLE_EPISODE,
    ASSEMBLE_FINAL,
    CHARACTER_LOCK,
    EPISODE_SCRIPT,
    LIPSYNC,
    MUSIC,
    QC_FINAL,
    SCRIPT_LOCK,
    SHOT_QC,
    STORYBOARD,
    TTS_TEMP,
    VIDEO,
    VOICE,
)
from app.pipeline.prompts import SCREEN_SYSTEM
from app.pipeline.schema import EpisodeSpec, SceneSpec, Screenplay, ShotSpec, clamp_screenplay, complete_json, extract_json
from app.pipeline.visual import apply_shot_prompt, normalize_anchors
from app.util import stable_seed


def bible_from_analysis(analysis: dict) -> dict:
    analysis = analysis or {}
    return {
        "characters": analysis.get("characters") or [],
        "world": analysis.get("tone") or analysis.get("genre") or "",
        "timeline": [analysis.get("summary") or ""],
        "phases": [],
    }


def reduce_bible(router, notes: list[dict]) -> dict:
    return complete_json(
        router,
        [
            {
                "role": "system",
                "content": "TASK:BIBLE_REDUCE\n把各章笔记收成故事圣经。只输出 json。示例：{\"title\":\"\",\"summary\":\"\",\"bible\":{\"characters\":[],\"world\":\"\",\"timeline\":[]}}",
            },
            {"role": "user", "content": json.dumps({"notes": notes}, ensure_ascii=False)},
        ],
        temperature=0.2,
        what="故事圣经",
    )


MAP_PROMPT_VERSION = "map-v3"
MAP_MAX_TOKENS = 400


def analysis_model(slot: dict | None) -> str | None:
    """Chapter notes stay on a fast chat model. The reasoner is for the script."""
    slot = slot or {}
    if slot.get("provider") != "openai_compatible":
        return None
    params = slot.get("params") or {}
    fast = (params.get("fast_model") or "").strip()
    if fast:
        return fast
    main = (slot.get("model") or "").lower()
    if "reasoner" in main or main.endswith("-r1"):
        return "deepseek-chat"
    return None


def chunk_key(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def cached_note(db, project_id: str, text: str) -> dict | None:
    from app.db.models import ChapterCache

    row = (
        db.query(ChapterCache)
        .filter(
            ChapterCache.project_id == project_id,
            ChapterCache.chunk_hash == chunk_key(text),
            ChapterCache.prompt_version == MAP_PROMPT_VERSION,
        )
        .one_or_none()
    )
    return dict(row.payload) if row is not None else None


def store_note(db, project_id: str, text: str, payload: dict) -> None:
    from app.db.models import ChapterCache

    db.add(
        ChapterCache(
            project_id=project_id,
            chunk_hash=chunk_key(text),
            prompt_version=MAP_PROMPT_VERSION,
            payload=payload,
        )
    )


def map_chapter(router, part: str, index: int, *, db=None, project_id: str | None = None, model: str | None = None) -> dict:
    if db is not None and project_id:
        cached = cached_note(db, project_id, part)
        if cached is not None:
            return cached
    payload = complete_json(
        router,
        [
            {
                "role": "system",
                "content": "TASK:BIBLE_MAP\n用很短的 json 记下人物、地点、时间。示例：{\"notes\":\"一句\",\"characters\":[],\"places\":[]}",
            },
            {"role": "user", "content": f"PART={index}\n<<<NOVEL\n{part}\n>>>"},
        ],
        temperature=0.2,
        model=model,
        max_tokens=MAP_MAX_TOKENS,
        what="章节笔记",
    )
    if db is not None and project_id:
        store_note(db, project_id, part, payload)
    return payload


def build_outline(router, bible: dict, chapters: list[str], episode_count: int, shots_per: int) -> list[dict]:
    payload = complete_json(
        router,
        [
            {
                "role": "system",
                "content": (
                    f"TASK:OUTLINE EPISODE_COUNT={episode_count} SHOTS_PER={shots_per}\n"
                    "按章节给出分集大纲。只输出 json。示例：{\"episodes\":[{\"number\":1,\"title\":\"\",\"synopsis\":\"\",\"chapter_index\":0}]}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"bible": bible, "chapter_count": len(chapters), "heads": [c[:80] for c in chapters]},
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.3,
        what="分集大纲",
    )
    episodes = payload.get("episodes") or []
    return episodes[: max(1, episode_count)]


def script_chapter(router, project, chapter: str, bible: dict, number: int, shots_per: int) -> Screenplay:
    messages = [
        {
            "role": "system",
            "content": (
                f"TASK:EPISODE EPISODE_NUMBER={number} MAX_EPISODES=1 MAX_SHOTS={shots_per}\n"
                + SCREEN_SYSTEM.format(max_episodes=1, max_shots=shots_per)
            ),
        },
        {
            "role": "user",
            "content": f"<<<NOVEL\n{chapter}\n>>>\n圣经：{json.dumps(bible, ensure_ascii=False)[:4000]}",
        },
    ]
    screenplay = complete_json(router, messages, validate=Screenplay.model_validate, temperature=0.6, what="剧本")
    screenplay = clamp_screenplay(screenplay, {"max_episodes": 1, "max_shots_per_episode": shots_per})
    if screenplay.episodes:
        screenplay.episodes[0].number = number
    return screenplay


def queue_remaining_scripts(db, project_id: str, screen_job_id: str) -> list[str]:
    from app.pipeline.graph import create_job

    ids = []
    episodes = (
        db.query(Episode)
        .filter(Episode.project_id == project_id, Episode.status == "outlined")
        .order_by(Episode.number)
        .all()
    )
    for episode in episodes:
        job = create_job(
            db,
            project_id=project_id,
            stage=EPISODE_SCRIPT,
            entity_type="episode",
            entity_id=episode.id,
            depends_on=[screen_job_id],
            key=f"episode_script:{episode.id}",
        )
        ids.append(job.id)
    return ids


def continue_episodes(db, project, count: int) -> list[str]:
    """Script the next outlined episodes. Their jobs do not wait on video."""
    from app.pipeline.graph import create_job

    screen = (
        db.query(Job)
        .filter(Job.project_id == project.id, Job.stage == "screenwrite", Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .first()
    )
    pending = (
        db.query(Episode)
        .filter(Episode.project_id == project.id, Episode.status == "outlined")
        .order_by(Episode.number)
        .limit(max(1, count))
        .all()
    )
    ids = []
    for episode in pending:
        job = create_job(
            db,
            project_id=project.id,
            stage=EPISODE_SCRIPT,
            entity_type="episode",
            entity_id=episode.id,
            depends_on=[screen.id] if screen is not None else [],
            key=f"episode_script:{episode.id}",
        )
        if job.status == "blocked" and not job.depends_on:
            job.status = "queued"
        ids.append(job.id)
    db.commit()
    from app.pipeline.graph import kick

    kick(project.id)
    return ids


def append_episode(db, project, screenplay: Screenplay) -> Episode:
    spec = screenplay.episodes[0]
    episode = (
        db.query(Episode)
        .filter(Episode.project_id == project.id, Episode.number == spec.number)
        .one_or_none()
    )
    if episode is None:
        episode = Episode(project_id=project.id, number=spec.number, title=spec.title, status="scripted")
        db.add(episode)
        db.flush()
    episode.title = spec.title or episode.title
    episode.hook = spec.hook
    episode.cliffhanger = spec.cliffhanger
    episode.synopsis = spec.synopsis
    episode.status = "scripted"
    for character in screenplay.characters:
        existing = (
            db.query(Character)
            .filter(Character.project_id == project.id, Character.name == character.name)
            .one_or_none()
        )
        if existing is None:
            appearance = character.appearance or character.description or character.name
            db.add(
                Character(
                    project_id=project.id,
                    name=character.name,
                    description=character.description or appearance,
                    appearance=appearance,
                    personality=character.personality,
                    voice_hint=character.voice_hint,
                    anchors=normalize_anchors(appearance, getattr(character, "anchors", None)),
                    seed=stable_seed(project.id, character.name),
                    status="approved",
                    locked=True,
                )
            )
    running = 1
    for scene_spec in spec.scenes:
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
                    number=running,
                    description=shot_spec.description,
                    dialogue=shot_spec.dialogue,
                    speaker=shot_spec.speaker,
                    camera=shot_spec.camera,
                    duration_sec=shot_spec.duration_sec,
                    visual_prompt=shot_spec.visual_prompt,
                    character_names=names,
                    stage_status={},
                    seed=stable_seed(project.id, str(spec.number), str(running)),
                    quality="draft" if (project.settings or {}).get("draft_mode") else "final",
                )
            )
            running += 1
    db.flush()
    for shot in db.query(Shot).filter(Shot.episode_id == episode.id).all():
        apply_shot_prompt(db, shot)
    return episode


def extend_graph(db, project_id: str, episode_id: str) -> None:
    from app.pipeline.graph import create_job

    lock = (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.stage == CHARACTER_LOCK)
        .one_or_none()
    )
    script_lock = (
        db.query(Job).filter(Job.project_id == project_id, Job.stage == SCRIPT_LOCK).one_or_none()
    )
    gate = (lock or script_lock).id
    script_gate = (script_lock or lock).id
    shots = db.query(Shot).filter(Shot.episode_id == episode_id).order_by(Shot.number).all()
    qc_ids = []
    for shot in shots:
        storyboard = create_job(
            db,
            project_id=project_id,
            stage=STORYBOARD,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[gate],
            key=f"storyboard:{shot.id}",
        )
        tts = create_job(
            db,
            project_id=project_id,
            stage=TTS_TEMP,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[script_gate],
            key=f"tts_temp:{shot.id}",
        )
        voice = create_job(
            db,
            project_id=project_id,
            stage=VOICE,
            entity_type="shot",
            entity_id=shot.id,
            depends_on=[script_gate],
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
        entity_id=episode_id,
        depends_on=[script_gate],
        key=f"music:{episode_id}",
    )
    assemble = create_job(
        db,
        project_id=project_id,
        stage=ASSEMBLE_EPISODE,
        entity_type="episode",
        entity_id=episode_id,
        depends_on=qc_ids + [music.id],
        key=f"assemble_episode:{episode_id}",
    )
    wanted = qc_ids + [music.id]
    if list(assemble.depends_on or []) != wanted:
        assemble.depends_on = wanted
        if assemble.status in {"failed", "succeeded"}:
            assemble.status = "blocked"
            assemble.enqueued = False
            assemble.error = None
    final = (
        db.query(Job).filter(Job.project_id == project_id, Job.stage == ASSEMBLE_FINAL).one_or_none()
    )
    if final is not None and assemble.id not in (final.depends_on or []):
        final.depends_on = list(final.depends_on or []) + [assemble.id]
        if final.status in {"succeeded", "failed"}:
            final.status = "blocked"
            final.enqueued = False
        qc_final = db.query(Job).filter(Job.project_id == project_id, Job.stage == QC_FINAL).one_or_none()
        if qc_final is not None and qc_final.status in {"succeeded", "failed"}:
            qc_final.status = "blocked"
            qc_final.enqueued = False


def merged_groups(shots: list[Shot], floor_sec: float = 15, ceiling_sec: float = 30) -> list[list[Shot]]:
    ordered = sorted(shots, key=lambda shot: (shot.scene_id or "", shot.number))
    groups: list[list[Shot]] = []
    current: list[Shot] = []
    total = 0.0
    for shot in ordered:
        span = float(shot.duration_sec or 2.5)
        if current and (current[-1].scene_id != shot.scene_id or total + span > ceiling_sec):
            groups.append(current)
            current = []
            total = 0.0
        current.append(shot)
        total += span
        if total >= floor_sec:
            groups.append(current)
            current = []
            total = 0.0
    if current:
        groups.append(current)
    return groups


def timestamped_prompt(group: list[Shot]) -> str:
    cursor = 0.0
    lines = []
    for shot in group:
        span = float(shot.duration_sec or 2.5)
        end = cursor + span
        spoken = f" {shot.speaker}：{shot.dialogue}" if shot.dialogue else ""
        lines.append(f"[{cursor:.1f}-{end:.1f}s] {shot.description}{spoken}")
        cursor = end
    return "\n".join(lines)


def lead_of(db, shot: Shot) -> tuple[Shot, list[Shot]] | None:
    shots = db.query(Shot).filter(Shot.episode_id == shot.episode_id).all()
    for group in merged_groups(shots):
        if any(item.id == shot.id for item in group) and len(group) > 1:
            return group[0], group
    return None
