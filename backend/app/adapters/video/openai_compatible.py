"""OpenAI-compatible video for a new-api style relay.

Create prefers POST {base}/video/generations and polls
GET {base}/video/generations/{task_id}. A 404 falls back to the older plural
path, then to OpenAI's POST /videos and GET /videos/{id}. The keyframe is sent
as `image`. A failed call raises; it does not invent a clip.
"""

import base64
import time

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import download, send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings

# (create path, poll path with {task_id})
_ROUTES = (
    ("/video/generations", "/video/generations/{task_id}"),
    ("/videos/generations", "/videos/generations/{task_id}"),
    ("/videos", "/videos/{task_id}"),
)
_SUCCESS = {"succeeded", "completed", "success", "succeed", "complete"}
_FAILURE = {"failure", "failed", "fail", "error", "cancelled", "canceled", "expired"}


class OpenAICompatibleVideo:
    provider = "openai_compatible"

    def __init__(self, client: httpx.Client | None = None, poll_interval: float = 5.0, poll_timeout: float = 600.0):
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = "seedance"
        self.client = client or httpx.Client(timeout=120)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def apply_route(self, route: dict) -> None:
        from app.settings_store import normalize_openai_base

        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = normalize_openai_base(route["base_url"])
        if route.get("model"):
            self.default_model = route["model"]

    def configure(self, params: dict | None):
        params = params or {}
        if params.get("poll_interval") is not None:
            self.poll_interval = float(params["poll_interval"])
        if params.get("poll_timeout") is not None:
            self.poll_timeout = float(params["poll_timeout"])
        return self

    def image_to_video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, model, last_image=None) -> MediaResult:
        if not self.api_key:
            raise ProviderNotConfigured("视频 API Key 是空的")
        from app.adapters.capabilities import clamp_video_request, hailuo_resolution, is_minimax

        chosen = model or self.default_model
        clamped = clamp_video_request(
            "openai_compatible",
            references=reference_images,
            last_image=last_image,
            duration_sec=duration_sec,
            merged=float(duration_sec or 0) > 10,
            model=chosen,
        )
        duration = int(clamped["duration_sec"])
        body: dict = {
            "model": chosen,
            "prompt": (prompt or "")[:1000],
            "duration": duration,
            "size": f"{int(width)}x{int(height)}",
            "width": int(width),
            "height": int(height),
        }
        image_ref = _image_ref(image)
        if image_ref:
            body["image"] = image_ref
        extras = [item for item in (_image_ref(ref) for ref in (clamped["references"] or [])) if item]
        if extras:
            body["images"] = extras
        metadata: dict = {}
        if is_minimax(chosen):
            resolution = hailuo_resolution(width, height)
            body["resolution"] = resolution
            metadata["resolution"] = resolution
        if seed is not None:
            metadata["seed"] = int(seed)
        if metadata:
            body["metadata"] = metadata
        created, poll_path = self._create(body)
        video_url = _video_url(created)
        status = _status(created)
        if not (video_url and status in _SUCCESS):
            task_id = _task_id(created)
            if not task_id:
                raise ProviderResponseError(f"视频任务没有返回编号：{str(created)[:300]}。请点重试。")
            finished = self._poll(str(task_id), poll_path)
            video_url = _video_url(finished)
            if not video_url:
                raise ProviderResponseError(f"视频完成了但没有地址（{task_id}）。请点重试。")
        data = download(self.client, video_url)
        if not data:
            raise ProviderResponseError("视频文件是空的。请点重试。")
        return MediaResult(
            data=data,
            mime="video/mp4",
            usage=Usage(video_seconds=duration),
            provider=self.provider,
            model=chosen,
            duration_sec=float(duration),
            width=width,
            height=height,
            seed=seed,
            extra={"task_id": _task_id(created), "notice": clamped["notice"], "reference_count": len(extras)},
        )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _create(self, body: dict) -> tuple[dict, str]:
        last_404: ProviderResponseError | None = None
        for create_path, poll_path in _ROUTES:
            try:
                created = send(
                    self.client,
                    "POST",
                    f"{self.base_url}{create_path}",
                    headers=self._headers(),
                    json=body,
                ).json()
            except ProviderResponseError as exc:
                if str(exc).startswith("404"):
                    last_404 = exc
                    continue
                raise ProviderResponseError(f"视频提交失败：{exc}。请点重试。") from exc
            return created, poll_path
        raise ProviderResponseError(f"视频接口不存在：{last_404}。请点重试。")

    def _poll(self, task_id: str, poll_path: str) -> dict:
        paths = [poll_path] + [item for _create, item in _ROUTES if item != poll_path]
        deadline = time.time() + self.poll_timeout
        while True:
            payload, used = self._poll_once(task_id, paths)
            paths = [used]
            status = _status(payload)
            if status in _SUCCESS:
                return payload
            if status in _FAILURE:
                detail = _error_text(payload) or status
                raise ProviderResponseError(f"视频生成失败：{detail}。请点重试。")
            if time.time() > deadline:
                raise ProviderResponseError(f"视频生成超时（{task_id}）。请点重试。")
            time.sleep(self.poll_interval)

    def _poll_once(self, task_id: str, paths: list[str]) -> tuple[dict, str]:
        last_404: ProviderResponseError | None = None
        for path in paths:
            url = f"{self.base_url}{path.format(task_id=task_id)}"
            try:
                payload = send(self.client, "GET", url, idempotent=True, headers=self._headers()).json()
            except ProviderResponseError as exc:
                if str(exc).startswith("404"):
                    last_404 = exc
                    continue
                raise ProviderResponseError(f"视频查询失败：{exc}。请点重试。") from exc
            return payload, path
        raise ProviderResponseError(f"视频查询路径不存在：{last_404}。请点重试。")


def _image_ref(value) -> str | None:
    if value is None or value == b"" or value == "":
        return None
    if isinstance(value, str):
        return value
    encoded = base64.b64encode(value).decode("ascii")
    return "data:image/png;base64," + encoded


def _task_id(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None
    for source in _objects(payload):
        for key in ("task_id", "id"):
            value = source.get(key)
            if value:
                return str(value)
    return None


def _status(payload: dict) -> str:
    for source in _objects(payload):
        value = source.get("status") or source.get("state")
        if value:
            return str(value).lower()
    return ""


def _video_url(payload: dict) -> str | None:
    for source in _objects(payload):
        for key in ("url", "video_url"):
            value = source.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        metadata = source.get("metadata")
        if isinstance(metadata, dict):
            for key in ("url", "video_url"):
                value = metadata.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
    return None


def _objects(payload: dict) -> list[dict]:
    found = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        found.append(data)
    elif isinstance(data, list):
        found.extend(item for item in data if isinstance(item, dict))
    task = payload.get("task")
    if isinstance(task, dict):
        found.append(task)
    return found


def _error_text(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")[:300]
    if error:
        return str(error)[:300]
    data = payload.get("data")
    if isinstance(data, dict) and data.get("error"):
        return _error_text(data)
    return str(payload.get("message") or "")[:300]
