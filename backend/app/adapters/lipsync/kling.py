"""Kling lip-sync.

POST {KLING_BASE_URL}/v1/videos/lip-sync
GET  {KLING_BASE_URL}/v1/videos/lip-sync/{task_id}

Audio-driven body:

    {"input": {"video_url": "...", "mode": "audio2video",
               "audio_type": "url"|"file", "audio_url"|"audio_file": "..."}}

video_url must be reachable by Kling (2-10s, 720p/1080p, mp4/mov). When the
object store can presign a public URL the stage passes it. audio_type=file
accepts raw base64. The newer /v1/videos/advanced-lip-sync flow needs a face
session first and is not the default.
"""

import base64
import time

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import download, send
from app.adapters.kling_auth import kling_headers
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class KlingLipSync:
    provider = "kling"

    def __init__(self, client: httpx.Client | None = None, poll_interval: float = 3.0, poll_timeout: float = 600.0):
        self.settings = get_settings()
        self.base_url = self.settings.kling_base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=120)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def configure(self, params: dict | None):
        params = params or {}
        if params.get("poll_interval") is not None:
            self.poll_interval = float(params["poll_interval"])
        if params.get("poll_timeout") is not None:
            self.poll_timeout = float(params["poll_timeout"])
        return self

    def sync(self, *, video, audio, video_url, audio_url, model) -> MediaResult:
        if not video_url:
            raise ProviderNotConfigured("Kling lip-sync needs a public video_url (set S3_PUBLIC_ENDPOINT)")
        payload_input: dict = {"video_url": video_url, "mode": "audio2video"}
        if audio_url:
            payload_input["audio_type"] = "url"
            payload_input["audio_url"] = audio_url
        elif audio:
            payload_input["audio_type"] = "file"
            payload_input["audio_file"] = base64.b64encode(audio).decode("ascii")
        else:
            raise ProviderNotConfigured("Kling lip-sync needs audio bytes or audio_url")
        created = send(
            self.client,
            "POST",
            f"{self.base_url}/v1/videos/lip-sync",
            headers=kling_headers(self.settings),
            json={"input": payload_input},
        ).json()
        if created.get("code") not in (0, None):
            raise ProviderResponseError(created.get("message") or "kling lip-sync create failed")
        data = created.get("data") or {}
        task_id = data.get("task_id")
        if not task_id:
            raise ProviderResponseError("kling lip-sync missing task_id")
        finished = self._poll(task_id)
        videos = ((finished.get("task_result") or {}).get("videos")) or []
        if not videos or not videos[0].get("url"):
            raise ProviderResponseError("kling lip-sync missing video url")
        blob = download(self.client, videos[0]["url"])
        seconds = float(videos[0].get("duration") or 0)
        return MediaResult(
            data=blob,
            mime="video/mp4",
            usage=Usage(lipsync_seconds=seconds),
            provider=self.provider,
            model=model or "kling-lip-sync",
            duration_sec=seconds,
            extra={"task_id": task_id},
        )

    def _poll(self, task_id: str) -> dict:
        deadline = time.time() + self.poll_timeout
        while True:
            payload = send(
                self.client,
                "GET",
                f"{self.base_url}/v1/videos/lip-sync/{task_id}",
                idempotent=True,
                headers=kling_headers(self.settings),
            ).json()
            data = payload.get("data") or {}
            status = str(data.get("task_status") or "").lower()
            if status in {"succeed", "succeeded", "success"}:
                return data
            if status in {"failed", "error"}:
                raise ProviderResponseError(data.get("task_status_msg") or "kling lip-sync failed")
            if time.time() > deadline:
                raise ProviderResponseError("kling lip-sync timeout")
            time.sleep(self.poll_interval)
