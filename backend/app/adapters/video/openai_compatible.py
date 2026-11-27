"""OpenAI-compatible video for a new-api style relay.

new-api's POST /video/generations often answers 503 model_not_found when the
group has no channel. OpenAI's POST /videos is the path that works on those
relays: multipart input_reference, then poll GET /videos/{id}, then download
GET /videos/{id}/content when the task completes without a url.

A base URL that has already succeeded remembers that create path so the next
shot does not pay for the dead route again.
"""

import base64
import threading
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
_PENDING = {"queued", "in_progress", "processing", "pending", "running", "submitted"}
_NO_CHANNEL = ("model_not_found", "无可用渠道", "no available channel", "no channel", "no_channel", "distributor")

_route_lock = threading.Lock()
_preferred_create: dict[str, str] = {}
_image_mode: dict[str, str] = {}
_multipart_bad: set[str] = set()


class _NextRoute(Exception):
    def __init__(self, reason: BaseException):
        self.reason = reason


class _ImageRejected(Exception):
    """This image submission was refused. Stay on /videos and try another mode."""

    def __init__(self, reason: BaseException):
        self.reason = reason


class OpenAICompatibleVideo:
    provider = "openai_compatible"

    def __init__(self, client: httpx.Client | None = None, poll_interval: float = 12.0, poll_timeout: float = 900.0):
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = "seedance"
        self.client = client or httpx.Client(timeout=180)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.on_progress = None

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

    def image_to_video(
        self,
        *,
        prompt,
        image,
        reference_images,
        duration_sec,
        seed,
        aspect_ratio,
        width,
        height,
        model,
        last_image=None,
        image_url: str | None = None,
        on_progress=None,
        resume_task_id: str | None = None,
        resume_poll_path: str | None = None,
    ) -> MediaResult:
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
        size = openai_video_size(width, height)
        image_bytes = image if isinstance(image, (bytes, bytearray)) and image else None
        new_api_body = _new_api_body(
            model=chosen,
            prompt=(prompt or "")[:1000],
            duration=duration,
            size=size,
            width=int(width),
            height=int(height),
            image=image,
            references=clamped["references"],
            seed=seed,
            minimax=is_minimax(chosen),
        )
        self.on_progress = on_progress
        if resume_task_id:
            task_id = str(resume_task_id)
            poll_path = resume_poll_path or "/videos/{task_id}"
            image_notice = ""
            self._note(task_id, poll_path)
            finished = self._poll(task_id, poll_path)
            data = self._fetch_video(task_id, poll_path, finished)
            notices = [part for part in (clamped["notice"], image_notice) if part]
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
                extra={"task_id": task_id, "notice": "；".join(notices), "resumed": True},
            )
        created, poll_path, image_notice = self._create(
            new_api_body,
            model=chosen,
            prompt=(prompt or "")[:1000],
            seconds=str(duration),
            size=size,
            image=bytes(image_bytes) if image_bytes else None,
            image_url=(image_url or "").strip() or None,
        )
        task_id = _task_id(created) or ""
        status = _status(created)
        video_url = _video_url(created)
        if task_id:
            self._note(task_id, poll_path)
        if not (video_url and status in _SUCCESS):
            if not task_id:
                raise ProviderResponseError(f"视频任务没有返回编号：{str(created)[:300]}。请点重试。")
            finished = self._poll(str(task_id), poll_path)
            data = self._fetch_video(str(task_id), poll_path, finished)
        else:
            self._note(task_id, poll_path)
            data = download(self.client, video_url)
        if not data:
            raise ProviderResponseError("视频文件是空的。请点重试。")
        notices = [part for part in (clamped["notice"], image_notice) if part]
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
            extra={"task_id": task_id, "notice": "；".join(notices), "reference_count": len(clamped["references"] or [])},
        )

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _json_headers(self) -> dict:
        return {**self._auth(), "Content-Type": "application/json"}

    def _create(
        self,
        new_api_body: dict,
        *,
        model: str,
        prompt: str,
        seconds: str,
        size: str,
        image: bytes | None,
        image_url: str | None,
    ) -> tuple[dict, str, str]:
        last: BaseException | None = None
        for create_path, poll_path in ordered_routes(self.base_url):
            try:
                if create_path == "/videos":
                    created, notice = self._create_openai_videos(
                        model=model, prompt=prompt, seconds=seconds, size=size, image=image, image_url=image_url
                    )
                else:
                    created = self._post_json(create_path, new_api_body)
                    notice = ""
            except _NextRoute as skipped:
                last = skipped.reason
                continue
            remember_route(self.base_url, create_path)
            return created, poll_path, notice
        raise ProviderResponseError(f"视频提交失败：{last}。请点重试。")

    def _create_openai_videos(
        self, *, model: str, prompt: str, seconds: str, size: str, image: bytes | None, image_url: str | None
    ) -> tuple[dict, str]:
        """Stay on /videos. A multipart 503 is an image-mode miss, not a dead route."""
        fields = {"model": model, "prompt": prompt, "seconds": seconds, "size": size}
        mode = image_mode_for(self.base_url)
        rejected = "首帧未被中转接受，本次改为纯文字生成视频"
        missing = "没有公网首帧地址，本次改为纯文字生成视频"

        def text_only(notice: str) -> tuple[dict, str]:
            return self._post_videos_json(fields), notice

        if image_url and mode != "text":
            try:
                created = self._post_videos_json({**fields, "image": image_url})
            except _ImageRejected:
                remember_image_mode(self.base_url, "text")
                return text_only(rejected)
            remember_image_mode(self.base_url, "url")
            return created, ""

        if mode == "text":
            notice = rejected if (image or image_url) else ""
            return text_only(notice)

        if image and not multipart_known_bad(self.base_url):
            try:
                return self._post_multipart("/videos", fields, image), ""
            except _ImageRejected:
                mark_multipart_bad(self.base_url)
        notice = missing if image else ""
        return text_only(notice)

    def _post_json(self, path: str, body: dict) -> dict:
        try:
            response = send(
                self.client,
                "POST",
                f"{self.base_url}{path}",
                headers=self._json_headers(),
                json=body,
            )
        except ProviderResponseError as exc:
            if _route_unavailable(exc):
                raise _NextRoute(exc) from exc
            raise ProviderResponseError(f"{exc}。请点重试。") from exc
        return response.json()

    def _post_videos_json(self, body: dict) -> dict:
        """JSON create on /videos. Image rejections stay on this route."""
        try:
            response = send(
                self.client,
                "POST",
                f"{self.base_url}/videos",
                headers=self._json_headers(),
                json=body,
            )
        except ProviderResponseError as exc:
            if body.get("image") and (_image_rejected(exc) or _route_unavailable(exc)):
                raise _ImageRejected(exc) from exc
            if _route_unavailable(exc):
                raise _NextRoute(exc) from exc
            raise ProviderResponseError(f"{exc}。请点重试。") from exc
        return response.json()

    def _post_multipart(self, path: str, fields: dict, image: bytes) -> dict:
        filename, mime = _image_file(image)
        try:
            response = send(
                self.client,
                "POST",
                f"{self.base_url}{path}",
                headers=self._auth(),
                data=fields,
                files={"input_reference": (filename, image, mime)},
            )
        except ProviderResponseError as exc:
            raise _ImageRejected(exc) from exc
        return response.json()

    def _note(self, task_id: str, poll_path: str) -> None:
        if self.on_progress is None or not task_id:
            return
        self.on_progress(task_id, poll_path)

    def _pause(self, step: int) -> None:
        if self.poll_interval <= 0:
            return
        delay = min(15.0, max(10.0, float(self.poll_interval)) + step * 2.5)
        time.sleep(delay)

    def _fetch_video(self, task_id: str, poll_path: str, finished: dict) -> bytes:
        self._note(task_id, poll_path)
        video_url = _video_url(finished)
        if video_url:
            return download(self.client, video_url)
        return self._download_content(task_id, poll_path)

    def _poll(self, task_id: str, poll_path: str) -> dict:
        paths = [poll_path] + [item for _create, item in _ROUTES if item != poll_path]
        deadline = time.time() + self.poll_timeout
        step = 0
        while True:
            self._note(task_id, poll_path)
            payload, used = self._poll_once(task_id, paths)
            paths = [used]
            status = _status(payload)
            if status in _SUCCESS or _progress_done(payload):
                return payload
            if status in _FAILURE:
                detail = _error_text(payload) or status
                raise ProviderResponseError(f"视频生成失败：{detail}。请点重试。")
            if time.time() > deadline:
                progress = _progress(payload)
                extra = f"，进度 {progress}%" if progress is not None else ""
                raise ProviderResponseError(f"视频生成超时（{task_id}{extra}）。请点重试。")
            self._pause(step)
            step += 1

    def _poll_once(self, task_id: str, paths: list[str]) -> tuple[dict, str]:
        last: ProviderResponseError | None = None
        for path in paths:
            url = f"{self.base_url}{path.format(task_id=task_id)}"
            try:
                payload = send(self.client, "GET", url, idempotent=True, headers=self._auth()).json()
            except ProviderResponseError as exc:
                if _route_unavailable(exc):
                    last = exc
                    continue
                raise ProviderResponseError(f"视频查询失败：{exc}。请点重试。") from exc
            return payload, path
        raise ProviderResponseError(f"视频查询路径不存在：{last}。请点重试。")

    def _download_content(self, task_id: str, poll_path: str) -> bytes:
        url = f"{self.base_url}{poll_path.format(task_id=task_id)}/content"
        try:
            response = send(self.client, "GET", url, idempotent=True, headers=self._auth(), timeout=180)
        except ProviderResponseError as exc:
            raise ProviderResponseError(f"视频完成了但下载失败：{exc}。请点重试。") from exc
        return response.content


def clear_route_cache() -> None:
    with _route_lock:
        _preferred_create.clear()
        _image_mode.clear()
        _multipart_bad.clear()


def image_mode_for(base_url: str) -> str:
    with _route_lock:
        return _image_mode.get(base_url.rstrip("/"), "")


def remember_image_mode(base_url: str, mode: str) -> None:
    with _route_lock:
        _image_mode[base_url.rstrip("/")] = mode


def multipart_known_bad(base_url: str) -> bool:
    with _route_lock:
        return base_url.rstrip("/") in _multipart_bad


def mark_multipart_bad(base_url: str) -> None:
    with _route_lock:
        _multipart_bad.add(base_url.rstrip("/"))


def remember_route(base_url: str, create_path: str) -> None:
    with _route_lock:
        _preferred_create[base_url.rstrip("/")] = create_path


def ordered_routes(base_url: str) -> list[tuple[str, str]]:
    with _route_lock:
        preferred = _preferred_create.get(base_url.rstrip("/"))
    routes = list(_ROUTES)
    if preferred:
        routes.sort(key=lambda item: 0 if item[0] == preferred else 1)
    return routes


def openai_video_size(width: int, height: int) -> str:
    """Portrait shots use the relay's vertical default. Landscape uses 1280x720."""
    if int(height or 0) >= int(width or 0):
        return "720x1280"
    return "1280x720"


def _route_unavailable(exc: BaseException) -> bool:
    text = str(exc)
    lowered = text.lower()
    code = lowered.split(" ", 1)[0]
    if code == "404":
        return True
    return any(token in text or token in lowered for token in _NO_CHANNEL)


def _image_rejected(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "fail_to_fetch_task" in text or "upstream rejected" in text or _client_error(exc)


def _client_error(exc: BaseException) -> bool:
    code = str(exc).split(" ", 1)[0]
    return code.isdigit() and code.startswith("4") and code != "404" and code != "429"


def _image_ref(value) -> str | None:
    if value is None or value == b"" or value == "":
        return None
    if isinstance(value, str):
        return value
    encoded = base64.b64encode(value).decode("ascii")
    return "data:image/png;base64," + encoded


def _image_file(data: bytes) -> tuple[str, str]:
    if data[:3] == b"\xff\xd8\xff":
        return "keyframe.jpg", "image/jpeg"
    return "keyframe.png", "image/png"


def _new_api_body(*, model, prompt, duration, size, width, height, image, references, seed, minimax: bool) -> dict:
    body: dict = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "size": size,
        "width": width,
        "height": height,
    }
    image_ref = _image_ref(image)
    if image_ref:
        body["image"] = image_ref
    extras = [item for item in (_image_ref(ref) for ref in (references or [])) if item]
    if extras:
        body["images"] = extras
    metadata: dict = {}
    if minimax:
        from app.adapters.capabilities import hailuo_resolution

        resolution = hailuo_resolution(width, height)
        body["resolution"] = resolution
        metadata["resolution"] = resolution
    if seed is not None:
        metadata["seed"] = int(seed)
    if metadata:
        body["metadata"] = metadata
    return body


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


def _progress(payload: dict) -> int | None:
    for source in _objects(payload):
        value = source.get("progress")
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _progress_done(payload: dict) -> bool:
    progress = _progress(payload)
    status = _status(payload)
    return progress is not None and progress >= 100 and status not in _FAILURE and status not in _PENDING


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
