from app.adapters.types import MediaResult, Usage
from app.media.audio_synth import synth_pad


class MockMusic:
    provider = "mock"

    def generate(self, *, prompt, duration_sec, model) -> MediaResult:
        duration = max(2.0, float(duration_sec or 8))
        data = synth_pad(duration)
        return MediaResult(
            data=data,
            mime="audio/wav",
            usage=Usage(video_seconds=duration),
            provider=self.provider,
            model=model or "mock-music",
            duration_sec=duration,
        )
