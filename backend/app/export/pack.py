"""Editorial handoff.

JianYing 5.9 and CapCut local drafts are plain JSON: `draft_content.json` on
Windows CapCut, `draft_info.json` on macOS and 剪映. Times are microseconds.
JianYing 6+ may encrypt drafts, so the same zip also carries an EDL, an FCPXML,
and an SRT. Assets use paths relative to the draft folder.
"""

import io
import json
import uuid
import zipfile
from xml.sax.saxutils import escape

from app.db.models import Artifact, Episode, Shot
from app.media.ffmpeg_tools import duration_of
from app.pipeline.artifacts import latest_artifact


def _us(seconds: float) -> int:
    return int(round(max(0.0, seconds) * 1_000_000))


def _timecode(seconds: float, fps: int = 12) -> str:
    frames = int(round(max(0.0, seconds) * fps))
    ff = frames % fps
    seconds_total = frames // fps
    ss = seconds_total % 60
    mm = (seconds_total // 60) % 60
    hh = seconds_total // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _shot_duration(store, artifact: Artifact | None, fallback: float) -> float:
    if artifact is None:
        return fallback
    try:
        data = store.get(artifact.storage_key)
    except Exception:
        return fallback
    path_holder = io.BytesIO(data)
    # Probe needs a file. Callers that already materialized can pass duration via meta.
    meta = artifact.meta or {}
    if meta.get("duration_sec"):
        return float(meta["duration_sec"])
    del path_holder
    return fallback


def collect_timeline(db, project, store) -> list[dict]:
    episodes = db.query(Episode).filter(Episode.project_id == project.id).order_by(Episode.number).all()
    clips = []
    cursor = 0.0
    for episode in episodes:
        shots = db.query(Shot).filter(Shot.episode_id == episode.id).order_by(Shot.number).all()
        music = latest_artifact(db, episode.id, "music")
        for shot in shots:
            video = latest_artifact(db, shot.id, "lipsync") or latest_artifact(db, shot.id, "video")
            voice = latest_artifact(db, shot.id, "voice")
            duration = float((video.meta or {}).get("duration_sec") or shot.duration_sec or 2.5) if video else shot.duration_sec
            if video is not None and not (video.meta or {}).get("duration_sec"):
                try:
                    import tempfile
                    from pathlib import Path

                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / "clip.mp4"
                        path.write_bytes(store.get(video.storage_key))
                        probed = duration_of(path)
                        if probed > 0.2:
                            duration = probed
                except Exception:
                    pass
            name = f"ep{episode.number:02d}-shot{shot.number:02d}"
            text = ""
            if (shot.dialogue or "").strip():
                prefix = f"{shot.speaker}：" if shot.speaker else ""
                text = prefix + shot.dialogue.strip()
            elif (shot.description or "").strip():
                text = shot.description.strip()[:28]
            clips.append(
                {
                    "name": name,
                    "start": cursor,
                    "duration": duration,
                    "text": text,
                    "video": video,
                    "voice": voice,
                    "episode_id": episode.id,
                }
            )
            cursor += duration
        episode_clips = [item for item in clips if item.get("episode_id") == episode.id and item.get("video") is not None]
        if music is not None and episode_clips:
            clips.append(
                {
                    "name": f"ep{episode.number:02d}-music",
                    "start": episode_clips[0]["start"],
                    "duration": sum(item["duration"] for item in episode_clips),
                    "text": "",
                    "video": None,
                    "voice": None,
                    "music": music,
                    "episode_id": episode.id,
                }
            )
    return clips


def _jianying_draft(project, clips, asset_names: dict[str, str]) -> dict:
    videos = []
    audios = []
    texts = []
    video_segments = []
    audio_segments = []
    text_segments = []
    total = 0.0
    for clip in clips:
        total = max(total, clip["start"] + clip["duration"])
        material_id = str(uuid.uuid4())
        segment_id = str(uuid.uuid4())
        target = {"start": _us(clip["start"]), "duration": _us(clip["duration"])}
        source = {"start": 0, "duration": _us(clip["duration"])}
        if clip.get("video") is not None:
            filename = asset_names[clip["video"].id]
            videos.append(
                {
                    "id": material_id,
                    "type": "video",
                    "path": filename,
                    "material_name": clip["name"],
                    "duration": _us(clip["duration"]),
                    "width": project.width,
                    "height": project.height,
                    "category_name": "local",
                }
            )
            video_segments.append(_segment(segment_id, material_id, target, source, volume=1.0))
        if clip.get("voice") is not None:
            audio_id = str(uuid.uuid4())
            audios.append(
                {
                    "id": audio_id,
                    "type": "extract_music",
                    "path": asset_names[clip["voice"].id],
                    "name": clip["name"] + "-voice",
                    "duration": _us(clip["duration"]),
                }
            )
            audio_segments.append(_segment(str(uuid.uuid4()), audio_id, target, source, volume=1.0))
        if clip.get("music") is not None:
            music_id = str(uuid.uuid4())
            audios.append(
                {
                    "id": music_id,
                    "type": "music",
                    "path": asset_names[clip["music"].id],
                    "name": clip["name"],
                    "duration": _us(clip["duration"]),
                }
            )
            audio_segments.append(_segment(str(uuid.uuid4()), music_id, target, source, volume=0.16))
        if clip.get("text"):
            text_id = str(uuid.uuid4())
            texts.append(
                {
                    "id": text_id,
                    "type": "text",
                    "content": json.dumps({"text": clip["text"], "styles": [{"range": [0, len(clip["text"])], "size": 8}]}, ensure_ascii=False),
                    "font_size": 8,
                    "alignment": 1,
                }
            )
            text_segments.append(_segment(str(uuid.uuid4()), text_id, target, source, volume=1.0))
    return {
        "id": project.id,
        "name": project.title,
        "duration": _us(total),
        "fps": project.fps or 12,
        "canvas_config": {"width": project.width, "height": project.height, "ratio": project.aspect_ratio},
        "platform": {"app_source": "lv", "app_version": "5.9.0", "os": "mac"},
        "tracks": [
            {"id": str(uuid.uuid4()), "type": "video", "segments": video_segments},
            {"id": str(uuid.uuid4()), "type": "audio", "segments": audio_segments},
            {"id": str(uuid.uuid4()), "type": "text", "segments": text_segments},
        ],
        "materials": {
            "videos": videos,
            "audios": audios,
            "texts": texts,
            "stickers": [],
            "video_effects": [],
            "material_animations": [],
            "transitions": [],
            "speeds": [],
            "canvases": [],
            "sound_channel_mappings": [],
        },
        "free_render_index_mode_on": False,
    }


def _segment(segment_id: str, material_id: str, target: dict, source: dict, volume: float) -> dict:
    return {
        "id": segment_id,
        "material_id": material_id,
        "target_timerange": target,
        "source_timerange": source,
        "speed": 1.0,
        "volume": volume,
        "visible": True,
        "reverse": False,
        "extra_material_refs": [],
        "clip": {
            "alpha": 1.0,
            "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0,
            "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": 0.0},
        },
    }


def _srt(clips: list[dict]) -> str:
    lines = []
    index = 1
    for clip in clips:
        if not clip.get("text"):
            continue
        lines.append(str(index))
        lines.append(f"{_srt_time(clip['start'])} --> {_srt_time(clip['start'] + clip['duration'])}")
        lines.append(clip["text"])
        lines.append("")
        index += 1
    return "\n".join(lines) + ("\n" if lines else "")


def _srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    s, milli = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _edl(project, clips: list[dict]) -> str:
    fps = project.fps or 12
    rows = [f"TITLE: {project.title}", "FCM: NON-DROP FRAME", ""]
    number = 1
    for clip in clips:
        if clip.get("video") is None:
            continue
        src_in = _timecode(0, fps)
        src_out = _timecode(clip["duration"], fps)
        rec_in = _timecode(clip["start"], fps)
        rec_out = _timecode(clip["start"] + clip["duration"], fps)
        rows.append(f"{number:03d}  AX       V     C        {src_in} {src_out} {rec_in} {rec_out}")
        rows.append(f"* FROM CLIP NAME: {clip['name']}.mp4")
        if clip.get("text"):
            rows.append(f"* COMMENT: {clip['text']}")
        rows.append("")
        number += 1
    return "\n".join(rows)


def _fcpxml(project, clips: list[dict], asset_names: dict[str, str]) -> str:
    fps = project.fps or 12
    assets = []
    spine = []
    for clip in clips:
        if clip.get("video") is None:
            continue
        asset_id = "a" + clip["name"].replace("-", "")
        filename = asset_names[clip["video"].id]
        assets.append(
            f'<asset id="{asset_id}" src="{escape(filename)}" hasVideo="1" hasAudio="1" '
            f'start="0s" duration="{clip["duration"]:.3f}s" name="{escape(clip["name"])}"/>'
        )
        spine.append(
            f'<asset-clip ref="{asset_id}" offset="{clip["start"]:.3f}s" duration="{clip["duration"]:.3f}s" '
            f'name="{escape(clip["name"])}" start="0s"/>'
        )
    total = 0.0
    if clips:
        total = max(item["start"] + item["duration"] for item in clips)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<fcpxml version="1.9">\n'
        "  <resources>\n"
        f'    <format id="fmt" frameDuration="1/{fps}s" width="{project.width}" height="{project.height}"/>\n'
        + "\n".join(f"    {line}" for line in assets)
        + "\n  </resources>\n"
        "  <library>\n"
        "    <event>\n"
        f'      <project name="{escape(project.title)}">\n'
        f'        <sequence format="fmt" duration="{total:.3f}s">\n'
        "          <spine>\n"
        + "\n".join(f"            {line}" for line in spine)
        + "\n          </spine>\n"
        "        </sequence>\n"
        "      </project>\n"
        "    </event>\n"
        "  </library>\n"
        "</fcpxml>\n"
    )


def _write_asset(archive: zipfile.ZipFile, store, artifact: Artifact, filename: str, names: dict) -> None:
    names[artifact.id] = filename
    archive.writestr(filename, store.get(artifact.storage_key))


def build_zip(db, project, store, kind: str) -> bytes:
    clips = collect_timeline(db, project, store)
    buffer = io.BytesIO()
    names: dict[str, str] = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for clip in clips:
            if clip.get("video") is not None and clip["video"].id not in names:
                _write_asset(archive, store, clip["video"], f"assets/{clip['name']}.mp4", names)
            if clip.get("voice") is not None and clip["voice"].id not in names:
                suffix = ".wav" if "wav" in (clip["voice"].content_type or "") else ".mp3"
                _write_asset(archive, store, clip["voice"], f"assets/{clip['name']}-voice{suffix}", names)
            if clip.get("music") is not None and clip["music"].id not in names:
                _write_asset(archive, store, clip["music"], f"assets/{clip['name']}.wav", names)
        srt = _srt(clips)
        archive.writestr("subtitles.srt", srt.encode("utf-8"))
        note = (
            "帧川导出。剪映 5.9 / CapCut 本地草稿是明文 JSON，时间单位是微秒。"
            "把整个文件夹放进剪映草稿目录后重启客户端。"
            "剪映 6 及更新版本可能加密草稿，打不开时用 timeline.fcpxml、timeline.edl 和 subtitles.srt。\n"
        )
        archive.writestr("README.txt", note.encode("utf-8"))
        if kind == "jianying":
            draft = _jianying_draft(project, clips, names)
            payload = json.dumps(draft, ensure_ascii=False, indent=2).encode("utf-8")
            archive.writestr("draft_content.json", payload)
            archive.writestr("draft_info.json", payload)
            archive.writestr(
                "draft_meta_info.json",
                json.dumps({"draft_id": project.id, "draft_name": project.title}, ensure_ascii=False).encode("utf-8"),
            )
        archive.writestr("timeline.edl", _edl(project, clips).encode("utf-8"))
        archive.writestr("timeline.fcpxml", _fcpxml(project, clips, names).encode("utf-8"))
    return buffer.getvalue()
