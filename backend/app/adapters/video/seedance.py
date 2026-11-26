"""Volcengine Ark Seedance image-to-video.

Create: POST {ARK_BASE_URL}/contents/generations/tasks
Poll:   GET  {ARK_BASE_URL}/contents/generations/tasks/{id}

The keyframe is sent as content.image_url with role=first_frame. Seedance 2.5
requires ratio=adaptive for first/last-frame tasks. Character reference sheets
are not mixed into that task type; set params.include_references=true to switch
to omni reference_image mode instead of locking the first frame.

Default model is doubao-seedance-2-0-260128. Seedance 2.5
(doubao-seedance-2-5-260628) is selected by changing the model id; 2.5 has
extra account requirements on Ark.
"""

import base64
import time

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import download, send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class SeedanceVideo:
    provider = "seedance"

    def __init__(self, client: httpx.Client | None = None, poll_interval: float = 3.0, poll_timeout: float = 600.0):
        settings = get_settings()
        self.api_key = settings.ark_api_key
        self.base_url = settings.ark_base_url.rstrip("/")
        self.default_model = settings.seedance_model
        self.client = client or httpx.Client(timeout=120)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.include_references = False

    def apply_route(self, route: dict) -> None:
        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = route["base_url"].rstrip("/")
        if route.get("model"):
            self.default_model = route["model"]

    def configure(self, params: dict | None):
        params = params or {}
        self.include_references = bool(params.get("include_references"))
        if params.get("poll_interval") is not None:
            self.poll_interval = float(params["poll_interval"])
        if params.get("poll_timeout") is not None:
            self.poll_timeout = float(params["poll_timeout"])
        return self

    def image_to_video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, model) -> MediaResult:
        if not self.api_key:
            raise ProviderNotConfigured("ARK_API_KEY is empty")
        chosen = model or self.default_model
        duration = max(4, min(12, int(round(float(duration_sec) or 5))))
        content: list[dict] = [{"type": "text", "text": prompt[:500]}]
        if self.include_references:
            content.append({"type": "image_url", "image_url": {"url": _data_uri(image)}, "role": "reference_image"})
            for ref in (reference_images or [])[:4]:
                content.append({"type": "image_url", "image_url": {"url": _data_uri(ref)}, "role": "reference_image"})
            ratio = "9:16" if height >= width else "16:9"
        else:
            content.append({"type": "image_url", "image_url": {"url": _data_uri(image)}, "role": "first_frame"})
            ratio = "adaptive"
        body: dict = {
            "model": chosen,
            "content": content,
            "resolution": "720p",
            "ratio": ratio,
            "duration": duration,
            "watermark": False,
        }
        if seed is not None:
            body["seed"] = int(seed)
        created = send(
            self.client,
            "POST",
            f"{self.base_url}/contents/generations/tasks",
            headers=self._headers(),
            json=body,
        ).json()
        task_id = created.get("id") or created.get("task_id") or (created.get("data") or {}).get("id")
        if not task_id:
            raise ProviderResponseError(f"seedance create missing id: {str(created)[:300]}")
        finished = self._poll(task_id)
        video_url = ((finished.get("content") or {}).get("video_url")) or finished.get("video_url")
        if not video_url:
            raise ProviderResponseError("seedance succeeded without video_url")
        data = download(self.client, video_url)
        usage_raw = finished.get("usage") or {}
        return MediaResult(
            data=data,
            mime="video/mp4",
            usage=Usage(video_seconds=float(finished.get("duration") or duration)),
            provider=self.provider,
            model=chosen,
            duration_sec=float(finished.get("duration") or duration),
            width=width,
            height=height,
            seed=finished.get("seed", seed),
            extra={"task_id": task_id, "completion_tokens": usage_raw.get("completion_tokens"), "reference_count": len(reference_images or [])},
        )

    def _poll(self, task_id: str) -> dict:
        deadline = time.time() + self.poll_timeout
        while True:
            payload = send(
                self.client,
                "GET",
                f"{self.base_url}/contents/generations/tasks/{task_id}",
                idempotent=True,
                headers=self._headers(),
            ).json()
            status = str(payload.get("status") or "").lower()
            if status == "succeeded":
                return payload
            if status in {"failed", "cancelled", "expired"}:
                raise ProviderResponseError(f"seedance {status}: {str(payload)[:400]}")
            if time.time() > deadline:
                raise ProviderResponseError(f"seedance poll timeout ({task_id})")
            time.sleep(self.poll_interval)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}


def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
