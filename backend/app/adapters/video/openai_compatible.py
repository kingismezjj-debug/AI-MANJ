"""OpenAI-compatible video generation for a relay.

POST {base}/videos/generations, then poll {base}/videos/generations/{id}
when the relay answers with a task id. A relay that only speaks chat and
images will fail this call and the router falls back to mock.
"""

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class OpenAICompatibleVideo:
    provider = "openai_compatible"

    def __init__(self, client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = "seedance"
        self.client = client or httpx.Client(timeout=120)

    def apply_route(self, route: dict) -> None:
        from app.settings_store import normalize_openai_base

        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = normalize_openai_base(route["base_url"])
        if route.get("model"):
            self.default_model = route["model"]

    def image_to_video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, model, last_image=None) -> MediaResult:
        if not self.api_key:
            raise ProviderNotConfigured("video API key is empty")
        from app.adapters.capabilities import clamp_video_request

        clamped = clamp_video_request(
            "openai_compatible",
            references=reference_images,
            last_image=last_image,
            duration_sec=duration_sec,
            merged=False,
        )
        chosen = model or self.default_model
        body = {
            "model": chosen,
            "prompt": (prompt or "")[:1000],
            "duration": int(clamped["duration_sec"]),
            "size": f"{width}x{height}",
        }
        created = send(
            self.client,
            "POST",
            f"{self.base_url}/videos/generations",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        ).json()
        if created.get("data"):
            return MediaResult(data=b"", mime="video/mp4", usage=Usage(video_seconds=body["duration"]), provider=self.provider, model=chosen, duration_sec=body["duration"], extra={"raw": str(created)[:200]})
        task_id = created.get("id") or (created.get("task") or {}).get("id")
        if not task_id:
            raise ProviderResponseError(f"video create missing id: {str(created)[:200]}")
        return MediaResult(
            data=b"",
            mime="video/mp4",
            usage=Usage(video_seconds=body["duration"]),
            provider=self.provider,
            model=chosen,
            duration_sec=body["duration"],
            extra={"task_id": task_id, "notice": clamped["notice"]},
        )
