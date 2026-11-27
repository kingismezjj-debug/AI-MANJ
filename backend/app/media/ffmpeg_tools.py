import json
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def run_ffmpeg(args: list[str]) -> None:
    if shutil.which("ffmpeg") is None:
        raise FFmpegError("ffmpeg is not installed")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        raise FFmpegError(detail or "ffmpeg failed")


def probe(path: str | Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise FFmpegError(proc.stderr.decode("utf-8", errors="replace")[-500:])
    return json.loads(proc.stdout.decode() or "{}")


def duration_of(path: str | Path) -> float:
    data = probe(path)
    try:
        return float(data.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def has_stream(path: str | Path, codec_type: str) -> bool:
    streams = probe(path).get("streams") or []
    return any(stream.get("codec_type") == codec_type for stream in streams)


def still_to_mp4(
    image_path: str,
    out_path: str,
    duration: float,
    width: int,
    height: int,
    audio_path: str | None = None,
) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},format=yuv420p,fps=12"
    )
    if audio_path:
        run_ffmpeg(
            [
                "-loop",
                "1",
                "-i",
                image_path,
                "-i",
                audio_path,
                "-t",
                f"{duration:.3f}",
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-shortest",
                "-movflags",
                "+faststart",
                out_path,
            ]
        )
        return
    run_ffmpeg(
        [
            "-loop",
            "1",
            "-i",
            image_path,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=44100",
            "-t",
            f"{duration:.3f}",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            "-movflags",
            "+faststart",
            out_path,
        ]
    )


def mux_audio(video_path: str, audio_path: str, out_path: str) -> None:
    run_ffmpeg(
        [
            "-i",
            video_path,
            "-i",
            audio_path,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            "-movflags",
            "+faststart",
            out_path,
        ]
    )


def concat_videos(paths: list[str], out_path: str) -> None:
    list_path = str(Path(out_path).with_suffix(".txt"))
    lines = []
    for path in paths:
        escaped = path.replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    Path(list_path).write_text("\n".join(lines), encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", "-movflags", "+faststart", out_path])


def mix_music(video_path: str, music_path: str, out_path: str) -> None:
    run_ffmpeg(
        [
            "-i",
            video_path,
            "-i",
            music_path,
            "-filter_complex",
            "[1:a]volume=0.16[m];[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            out_path,
        ]
    )


def burn_ass(video_path: str, ass_path: str, fontsdir: str, out_path: str) -> None:
    vf = f"ass={ass_path}:fontsdir={fontsdir}"
    run_ffmpeg(
        [
            "-i",
            video_path,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            out_path,
        ]
    )
