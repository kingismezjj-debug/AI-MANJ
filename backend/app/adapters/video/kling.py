"""Kling image-to-video.

Legacy shape, still the documented contract for models selected by model_name
(v1 through v3-omni):

    POST {KLING_BASE_URL}/v1/videos/image2video
    GET  {KLING_BASE_URL}/v1/videos/image2video/{task_id}

Auth: Authorization: Bearer <KLING_API_KEY>.
If only an older AK/SK pair is configured, a 30-minute HS256 JWT is signed
(iss=access key, exp=now+1800, nbf=now-5). New path-style models reject AK/SK;
put the console API key in KLING_API_KEY for those.

The keyframe is raw base64 without a data: prefix. Duration is "5" or "10".
"""

import base64
import time

import httpx

from app.adapters.errors import ProviderResponseError
from app.adapters.http import download, send
from app.adapters.kling_auth import kling_headers
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class KlingVideo:
    provider = "kling"

    def __init__(self, client: httpx.Client | None = None, poll_interval: float = 3.0, poll_timeout: float = 600.0):
        self.settings = get_settings()
        self.base_url = self.settings.kling_base_url.rstrip("/")
        self.default_model = self.settings.kling_video_model
        self.api_key = self.settings.kling_api_key
        self.client = client or httpx.Client(timeout=120)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.create_path = "/v1/videos/image2video"

    def apply_route(self, route: dict) -> None:
        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = route["base_url"].rstrip("/")
        if route.get("model"):
            self.default_model = route["model"]

    def configure(self, params: dict | None):
        params = params or {}
        if params.get("create_path"):
            self.create_path = params["create_path"]
        if params.get("poll_interval") is not None:
            self.poll_interval = float(params["poll_interval"])
        if params.get("poll_timeout") is not None:
            self.poll_timeout = float(params["poll_timeout"])
        return self

    def image_to_video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, model, last_image=None) -> MediaResult:
        from app.adapters.capabilities import clamp_video_request

        clamped = clamp_video_request("kling", references=reference_images, last_image=last_image, duration_sec=duration_sec, merged=False)
        self.notice = clamped["notice"]
        chosen = model or self.default_model
        duration = "5" if float(duration_sec) <= 5 else "10"
        body = {
            "model_name": chosen,
            "image": base64.b64encode(image).decode("ascii"),
            "prompt": prompt[:2000],
            "negative_prompt": "low quality, extra limbs, text, watermark",
            "cfg_scale": 0.5,
            "mode": "std",
            "aspect_ratio": aspect_ratio or "9:16",
            "duration": duration,
        }
        created = send(
            self.client,
            "POST",
            f"{self.base_url}{self.create_path}",
            headers=self._headers(),
            json=body,
        ).json()
        data = created.get("data") or {}
        if created.get("code") not in (0, None):
            raise ProviderResponseError(f"kling create failed: {created.get('message')}")
        task_id = data.get("task_id") or created.get("task_id")
        if not task_id:
            raise ProviderResponseError("kling create missing task_id")
        finished = self._poll(task_id)
        videos = ((finished.get("task_result") or {}).get("videos")) or []
        if not videos or not videos[0].get("url"):
            raise ProviderResponseError("kling succeeded without video url")
        blob = download(self.client, videos[0]["url"])
        seconds = float(videos[0].get("duration") or duration)
        return MediaResult(
            data=blob,
            mime="video/mp4",
            usage=Usage(video_seconds=seconds),
            provider=self.provider,
            model=chosen,
            duration_sec=seconds,
            width=width,
            height=height,
            seed=seed,
            extra={"task_id": task_id, "reference_count": len(reference_images or [])},
        )

    def _headers(self) -> dict:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        return kling_headers(self.settings)

    def _poll(self, task_id: str) -> dict:
        deadline = time.time() + self.poll_timeout
        while True:
            payload = send(
                self.client,
                "GET",
                f"{self.base_url}{self.create_path}/{task_id}",
                idempotent=True,
                headers=self._headers(),
            ).json()
            data = payload.get("data") or {}
            status = str(data.get("task_status") or payload.get("task_status") or "").lower()
            if status in {"succeed", "succeeded", "success"}:
                return data or payload
            if status in {"failed", "error"}:
                raise ProviderResponseError(data.get("task_status_msg") or "kling failed")
            if time.time() > deadline:
                raise ProviderResponseError(f"kling poll timeout ({task_id})")
            time.sleep(self.poll_interval)
