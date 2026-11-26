"""MiniMax speech T2A v2.

POST {MINIMAX_BASE_URL}/v1/t2a_v2
Mainland default host is https://api.minimaxi.com (overseas: https://api.minimax.io).
Non-streaming responses put hex audio in data.audio when output_format is hex.
"""

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class MiniMaxTTS:
    provider = "minimax"

    def __init__(self, client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = settings.minimax_api_key
        self.base_url = settings.minimax_base_url.rstrip("/")
        self.default_model = settings.minimax_model
        self.default_voice = settings.minimax_voice_id
        self.client = client or httpx.Client(timeout=120)

    def apply_route(self, route: dict) -> None:
        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = route["base_url"].rstrip("/")
        if route.get("model"):
            self.default_model = route["model"]

    def synthesize(self, *, text, voice, speed, model) -> MediaResult:
        if not self.api_key:
            raise ProviderNotConfigured("MINIMAX_API_KEY is empty")
        chosen = model or self.default_model
        voice_id = voice or self.default_voice
        if voice_id in {"male", "female"}:
            voice_id = self.default_voice
        body = {
            "model": chosen,
            "text": text,
            "stream": False,
            "output_format": "hex",
            "voice_setting": {
                "voice_id": voice_id,
                "speed": float(speed or 1),
                "vol": 1,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        response = send(
            self.client,
            "POST",
            f"{self.base_url}/v1/t2a_v2",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        payload = response.json()
        base = payload.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            raise ProviderResponseError(f"minimax {base.get('status_code')}: {base.get('status_msg')}")
        hex_audio = (payload.get("data") or {}).get("audio")
        if not hex_audio:
            raise ProviderResponseError("minimax response missing data.audio")
        data = bytes.fromhex(hex_audio)
        return MediaResult(
            data=data,
            mime="audio/mpeg",
            usage=Usage(audio_chars=len(text or "")),
            provider=self.provider,
            model=chosen,
            duration_sec=0,
        )
