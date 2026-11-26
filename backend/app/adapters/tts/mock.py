from app.adapters.types import MediaResult, Usage
from app.media.audio_synth import synth_wav


class MockTTS:
    provider = "mock"

    def synthesize(self, *, text, voice, speed, model) -> MediaResult:
        length = max(len(text or ""), 1)
        duration = max(1.2, min(6.0, 0.18 * length))
        # The pipeline passes an exact shot duration via speed's sibling; callers
        # that set model params duration_sec override this heuristic.
        freq = 196.0 if (voice or "").lower().startswith("m") or voice == "male" else 262.0
        if speed and speed < 1:
            freq *= 0.9
        data = synth_wav(duration, freq=freq, volume=0.35)
        return MediaResult(
            data=data,
            mime="audio/wav",
            usage=Usage(audio_chars=len(text or "")),
            provider=self.provider,
            model=model or "mock-tts",
            duration_sec=duration,
        )
