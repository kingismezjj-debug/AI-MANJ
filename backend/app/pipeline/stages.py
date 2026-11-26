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
from app.media.audio_synth import synth_wav
from app.media.ffmpeg_tools import burn_ass, concat_videos, duration_of, has_stream, mix_music, still_to_mp4
from app.media.fonts import find_cjk_font
from app.media.images import STYLE_SUFFIX, fit_png
from app.media.subtitles import build_ass
from app.novel.parse import chunk_text
from app.pipeline.artifacts import latest_artifact, materialize, save_bytes, temp_workspace
from app.pipeline.graph import build_graph, ensure_screen_job, schedule_video_regen
from app.pipeline.names import (
    ANIMATIC,
    ASSEMBLE_EPISODE,
    ASSEMBLE_FINAL,
    CHARACTER_BIBLE,
    CHARACTER_LOCK,
    INGEST,
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
from app.pipeline.prompts import ANALYSIS_SYSTEM, screen_system
from app.pipeline.schema import clamp_screenplay, extract_json, parse_screenplay
from app.util import stable_seed


class HoldForApproval(Exception):
    """Character sheets exist, but a person still has to lock them."""


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
    handler = HANDLERS[job.stage]
    if job.entity_type == "shot":
        shot = db.get(Shot, job.entity_id)
        if shot is not None:
            mark_shot(shot, job.stage, "running")
    return handler(db, job, project)


def _router(db, project, job) -> ModelRouter:
    return ModelRouter(db, project, job.id)


def _store():
    from app.storage import get_store

    return get_store()


def run_ingest(db: Session, job: Job, project: Project) -> dict:
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
    if not (novel.text or "").strip():
        raise RuntimeError("novel text is empty")
    db.query(Chunk).filter(Chunk.novel_id == novel.id).delete()
    pieces = chunk_text(novel.text)
    for index, piece in enumerate(pieces):
        db.add(Chunk(novel_id=novel.id, idx=index, text=piece))
    db.flush()
    result = _router(db, project, job).llm(
        [
            {"role": "system", "content": ANALYSIS_SYSTEM},
            {"role": "user", "content": f"<<<NOVEL\n{novel.text[:12000]}\n>>>"},
        ],
        json_mode=True,
        temperature=0.3,
    )
    novel.analysis = extract_json(result.text)
    ensure_screen_job(db, project.id, job.id)
    return {"chunks": len(pieces), "title": novel.analysis.get("title")}


def _novel_for_writer(novel: Novel, db: Session) -> str:
    if len(novel.text) <= 12000:
        return novel.text
    chunks = db.query(Chunk).filter(Chunk.novel_id == novel.id).order_by(Chunk.idx).all()
    heads = "\n".join(chunk.text[:180] for chunk in chunks[:40])
    return f"【分段摘录】\n{heads}\n【开篇】\n{novel.text[:4000]}"


def _complete_screenplay(router: ModelRouter, project: Project, novel_text: str, analysis: dict):
    messages = [
        {"role": "system", "content": screen_system(project.settings or {})},
        {
            "role": "user",
            "content": f"<<<NOVEL\n{novel_text}\n>>>\n分析：{json.dumps(analysis or {}, ensure_ascii=False)}",
        },
    ]
    result = router.llm(messages, json_mode=True, temperature=0.7)
    try:
        return parse_screenplay(result.text)
    except ValueError:
        repair = router.llm(
            [
                {"role": "system", "content": "只输出合法 JSON，不要 Markdown。"},
                {"role": "user", "content": result.text[:8000]},
            ],
            json_mode=True,
            temperature=0,
        )
        return parse_screenplay(repair.text)


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
                    )
                )
                running_number += 1
                shot_count += 1
    db.flush()
    return {"episodes": len(screenplay.episodes), "shots": shot_count, "title": screenplay.title}


def run_screenwrite(db: Session, job: Job, project: Project) -> dict:
    novel = db.query(Novel).filter(Novel.project_id == project.id).one()
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
    return summary


def run_character_bible(db: Session, job: Job, project: Project) -> dict:
    character = db.get(Character, job.entity_id)
    if character is None:
        raise RuntimeError("character missing")
    prompt = f"角色定妆设定图，正面半身与全身，纯色背景，{character.appearance}。{STYLE_SUFFIX}"
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
        raise HoldForApproval("waiting for character approval")
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
    prompt = f"{shot.visual_prompt}\n{location_note}景别：{shot.camera}。{STYLE_SUFFIX}"
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
        meta={"reference_count": len(refs), "provider": result.provider, "seed": shot.seed},
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
    return result.data, result.mime, {"provider": result.provider, "chars": len(text)}


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
    shot = db.get(Shot, job.entity_id)
    store = _store()
    frame = latest_artifact(db, shot.id, "keyframe")
    if frame is None:
        raise RuntimeError("keyframe missing")
    refs = _refs_for_shot(db, store, shot)
    result = _router(db, project, job).video(
        prompt=shot.visual_prompt or shot.description,
        image=store.get(frame.storage_key),
        reference_images=refs,
        duration_sec=shot.duration_sec,
        seed=shot.seed,
        aspect_ratio=project.aspect_ratio,
        width=project.width,
        height=project.height,
    )
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
        meta={"provider": result.provider, "reference_count": len(refs), "duration_sec": result.duration_sec},
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
    return {"ok": True, "artifact_id": final.id, "duration_sec": duration}


HANDLERS = {
    INGEST: run_ingest,
    SCREENWRITE: run_screenwrite,
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
