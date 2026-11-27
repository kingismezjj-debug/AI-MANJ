"""Publish a shot keyframe as a JPEG the video relay can fetch.

Files live in a directory that only the frame server reads. Names are long
and random. Anything older than 24 hours is deleted. The public base URL is
a manual setting, PUBLIC_ASSET_BASE_URL, or a cloudflared quick tunnel that
points only at that file server.
"""

import re
import secrets
import time
from io import BytesIO
from pathlib import Path

import httpx

_TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_discovered = {"url": "", "at": 0.0}


def frame_label(db=None) -> str:
    return "首帧公网地址：可用" if public_frame_base(db) else "首帧公网地址：不可用"


def public_frame_base(db=None, project_id: str = "") -> str:
    saved = _saved_base(db, project_id)
    if saved:
        return saved
    from app.config import get_settings

    env = (get_settings().public_asset_base_url or "").strip().rstrip("/")
    if env:
        return env
    return discover_tunnel()


def publish_keyframe(data: bytes, *, width: int, height: int, db=None, project_id: str = "") -> str | None:
    base = public_frame_base(db, project_id)
    if not base or not data:
        return None
    folder = _publish_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    _sweep(folder)
    name = secrets.token_urlsafe(24) + ".jpg"
    path = folder / name
    path.write_bytes(to_jpeg(data, width, height))
    return f"{base}/{name}"


def to_jpeg(data: bytes, width: int, height: int, limit: int = 2_000_000) -> bytes:
    from PIL import Image

    image = Image.open(BytesIO(data)).convert("RGB")
    target_w, target_h = (720, 1280) if int(height or 0) >= int(width or 0) else (1280, 720)
    target_ratio = target_w / target_h
    ratio = image.width / max(1, image.height)
    if ratio > target_ratio:
        kept = int(image.height * target_ratio)
        left = max(0, (image.width - kept) // 2)
        image = image.crop((left, 0, left + kept, image.height))
    else:
        kept = int(image.width / target_ratio)
        top = max(0, (image.height - kept) // 2)
        image = image.crop((0, top, image.width, top + kept))
    image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    quality = 85
    out = BytesIO()
    while True:
        out.seek(0)
        out.truncate()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        if out.tell() <= limit or quality <= 40:
            return out.getvalue()
        quality -= 15


def discover_tunnel(url: str | None = None) -> str:
    now = time.time()
    if _discovered["url"] and now - _discovered["at"] < 30:
        return _discovered["url"]
    from app.config import get_settings

    endpoint = (url if url is not None else get_settings().cloudflared_metrics_url or "").strip()
    if not endpoint:
        return ""
    found = _fetch_tunnel(endpoint)
    if not found and endpoint.rstrip("/").endswith("/quicktunnel"):
        found = _fetch_tunnel(endpoint.rstrip("/")[: -len("/quicktunnel")] + "/metrics")
    if found:
        _discovered["url"] = found
        _discovered["at"] = now
    return found


def parse_tunnel_payload(text: str) -> str:
    text = text or ""
    try:
        import json

        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        for key in ("url", "hostname"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return _as_https(value)
        result = payload.get("result")
        if isinstance(result, dict):
            for key in ("url", "hostname"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    return _as_https(value)
    match = _TUNNEL.search(text)
    return match.group(0) if match else ""


def _fetch_tunnel(url: str) -> str:
    try:
        response = httpx.get(url, timeout=2)
    except Exception:
        return ""
    if response.status_code >= 400:
        return ""
    return parse_tunnel_payload(response.text)


def _as_https(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return "https://" + value


def _saved_base(db, project_id: str) -> str:
    if db is None:
        return ""
    from app.db.models import ApiSlot

    scopes = []
    if project_id:
        scopes.append(("project", project_id))
    scopes.append(("global", ""))
    for scope, pid in scopes:
        row = (
            db.query(ApiSlot)
            .filter(ApiSlot.scope == scope, ApiSlot.project_id == pid, ApiSlot.category == "video")
            .one_or_none()
        )
        saved = ((row.params or {}).get("public_asset_base_url") if row else "") or ""
        saved = str(saved).strip().rstrip("/")
        if saved:
            return saved
    return ""


def _publish_dir() -> Path:
    from app.config import get_settings

    return Path(get_settings().frame_publish_dir)


def _sweep(folder: Path) -> None:
    cutoff = time.time() - 24 * 60 * 60
    for path in folder.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
