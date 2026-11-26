"""ElevenLabs text-to-speech.

POST {ELEVENLABS_BASE_URL}/v1/text-to-speech/{voice_id}
Header: xi-api-key. The body returns audio bytes (mpeg) directly.
eleven_multilingual_v2 is the default because dialogue is Chinese.
"""

import httpx

from app.adapters.errors import ProviderNotConfigured
from app.adapters.http import send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class ElevenLabsTTS:
    provider = "elevenlabs"

    def __init__(self, client: httpx.Client | None = None):
        settings = get_settings()
        if not settings.elevenlabs_api_key:
            raise ProviderNotConfigured("ELEVENLABS_API_KEY is empty")
        self.api_key = settings.elevenlabs_api_key
        self.base_url = settings.elevenlabs_base_url.rstrip("/")
        self.default_model = settings.elevenlabs_model
        self.default_voice = settings.elevenlabs_voice_id
        self.client = client or httpx.Client(timeout=120)

    def synthesize(self, *, text, voice, speed, model) -> MediaResult:
        chosen = model or self.default_model
        voice_id = voice or self.default_voice
        if voice_id in {"male", "female", ""}:
            voice_id = self.default_voice
        response = send(
            self.client,
            "POST",
            f"{self.base_url}/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
            json={"text": text, "model_id": chosen},
        )
        return MediaResult(
            data=response.content,
            mime="audio/mpeg",
            usage=Usage(audio_chars=len(text or "")),
            provider=self.provider,
            model=chosen,
        )
