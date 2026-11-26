import tempfile
from pathlib import Path

from app.adapters.types import MediaResult, Usage
from app.media.ffmpeg_tools import duration_of, mux_audio


class MockLipSync:
    provider = "mock"

    def sync(self, *, video, audio, video_url, audio_url, model) -> MediaResult:
        with tempfile.TemporaryDirectory(prefix="zc-lip-") as folder:
            root = Path(folder)
            video_path = root / "in.mp4"
            audio_path = root / "speech.bin"
            out = root / "out.mp4"
            video_path.write_bytes(video)
            audio_path.write_bytes(audio)
            mux_audio(str(video_path), str(audio_path), str(out))
            data = out.read_bytes()
            duration = duration_of(out)
        return MediaResult(
            data=data,
            mime="video/mp4",
            usage=Usage(lipsync_seconds=duration),
            provider=self.provider,
            model=model or "mock-lipsync",
            duration_sec=duration,
            extra={"mode": "mux"},
        )
