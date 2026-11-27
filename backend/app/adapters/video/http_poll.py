"""Config-driven async video adapter.

Use this when a new vendor is "POST a job, poll until a status field flips,
download a url" and you do not want a Python class yet. Point the project's
video route at provider "http_poll" and fill params:

    create_url, poll_url (with {task_id}), task_id_path, status_path,
    success_values, failure_values, video_url_path, auth_env, body

String values that are exactly "{name}" keep the Python type of the context
value, so duration can stay an integer. See the README for a full example.
"""

import re
import time

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import download, send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings
from app.util import dotted_get


class HttpPollVideo:
    provider = "http_poll"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=120)
        self.params: dict = {}

    def configure(self, params: dict | None):
        self.params = dict(params or {})
        if not self.params.get("create_url") or not self.params.get("poll_url"):
            raise ProviderNotConfigured("http_poll requires create_url and poll_url")
        return self

    def image_to_video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, model, last_image=None) -> MediaResult:
        import base64

        ctx = {
            "prompt": prompt,
            "model": model or self.params.get("model") or "http-video",
            "image_b64": base64.b64encode(image).decode("ascii"),
            "image_data_uri": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
            "duration": int(round(float(duration_sec) or 5)),
            "seed": int(seed or 0),
            "aspect_ratio": aspect_ratio or "9:16",
            "width": width,
            "height": height,
        }
        body = _render(self.params.get("body") or {"prompt": "{prompt}"}, ctx)
        headers = {"Content-Type": "application/json", **(self.params.get("headers") or {})}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        created = send(self.client, "POST", self.params["create_url"], headers=headers, json=body).json()
        task_id = dotted_get(created, self.params.get("task_id_path") or "id")
        if not task_id:
            raise ProviderResponseError(f"http_poll create missing task id: {str(created)[:300]}")
        finished = self._poll(str(task_id), headers)
        video_url = dotted_get(finished, self.params.get("video_url_path") or "video_url")
        if not video_url:
            raise ProviderResponseError("http_poll missing video url")
        data = download(self.client, video_url)
        seconds = float(ctx["duration"])
        return MediaResult(
            data=data,
            mime="video/mp4",
            usage=Usage(video_seconds=seconds),
            provider=self.provider,
            model=str(ctx["model"]),
            duration_sec=seconds,
            width=width,
            height=height,
            seed=seed,
            extra={"task_id": task_id, "reference_count": len(reference_images or [])},
        )

    def _poll(self, task_id: str, headers: dict) -> dict:
        interval = float(self.params.get("poll_interval") or 3)
        timeout = float(self.params.get("poll_timeout") or 600)
        success = {str(item).lower() for item in (self.params.get("success_values") or ["succeeded", "succeed", "success"])}
        failure = {str(item).lower() for item in (self.params.get("failure_values") or ["failed", "error", "cancelled"])}
        status_path = self.params.get("status_path") or "status"
        deadline = time.time() + timeout
        url = self.params["poll_url"].replace("{task_id}", task_id)
        while True:
            payload = send(self.client, "GET", url, idempotent=True, headers=headers).json()
            status = str(dotted_get(payload, status_path) or "").lower()
            if status in success:
                return payload
            if status in failure:
                raise ProviderResponseError(f"http_poll {status}")
            if time.time() > deadline:
                raise ProviderResponseError("http_poll timeout")
            time.sleep(interval)

    def _token(self) -> str:
        env_name = self.params.get("auth_env") or "VIDEO_HTTP_API_KEY"
        settings = get_settings()
        if env_name == "VIDEO_HTTP_API_KEY":
            return settings.video_http_api_key
        import os

        return os.environ.get(env_name, "")


def _render(value, ctx: dict):
    if isinstance(value, dict):
        return {key: _render(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, ctx) for item in value]
    if isinstance(value, str):
        exact = re.fullmatch(r"\{(\w+)\}", value)
        if exact and exact.group(1) in ctx:
            return ctx[exact.group(1)]

        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in ctx:
                return match.group(0)
            return str(ctx[key])

        return re.sub(r"\{(\w+)\}", replace, value)
    return value
