import tempfile
from pathlib import Path

from app.adapters.types import MediaResult, Usage
from app.media.ffmpeg_tools import still_to_mp4


class MockVideo:
    provider = "mock"

    def image_to_video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, model) -> MediaResult:
        with tempfile.TemporaryDirectory(prefix="zc-vid-") as folder:
            root = Path(folder)
            frame = root / "frame.png"
            out = root / "clip.mp4"
            frame.write_bytes(image)
            still_to_mp4(str(frame), str(out), duration=max(0.8, float(duration_sec)), width=width, height=height)
            data = out.read_bytes()
        return MediaResult(
            data=data,
            mime="video/mp4",
            usage=Usage(video_seconds=float(duration_sec)),
            provider=self.provider,
            model=model or "mock-video",
            duration_sec=float(duration_sec),
            width=width,
            height=height,
            seed=seed,
            extra={"reference_count": len(reference_images or [])},
        )
