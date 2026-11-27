"""Public keyframe files and tunnel discovery. No network and no relay."""

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from app.config import get_settings
from app.pipeline import frames
from app.pipeline.frames import discover_tunnel, frame_label, parse_tunnel_payload, publish_keyframe, to_jpeg


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    frames._discovered["url"] = ""
    frames._discovered["at"] = 0
    get_settings.cache_clear()
    yield
    frames._discovered["url"] = ""
    frames._discovered["at"] = 0
    get_settings.cache_clear()


def _png() -> bytes:
    image = Image.new("RGB", (40, 80), (20, 30, 40))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_jpeg_matches_portrait_and_stays_small():
    data = to_jpeg(_png(), 720, 1280)
    assert data[:2] == b"\xff\xd8"
    assert len(data) < 2_000_000
    opened = Image.open(io.BytesIO(data))
    assert opened.size == (720, 1280)


def test_publish_uses_unguessable_name_and_deletes_old_files(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAME_PUBLISH_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ASSET_BASE_URL", "https://frames.example")
    monkeypatch.setenv("CLOUDFLARED_METRICS_URL", "")
    get_settings.cache_clear()
    stale = tmp_path / "old-frame-name-long-enough.jpg"
    stale.write_bytes(b"old")
    old = time.time() - 25 * 60 * 60
    import os

    os.utime(stale, (old, old))
    url = publish_keyframe(_png(), width=720, height=1280)
    assert url.startswith("https://frames.example/")
    name = url.rsplit("/", 1)[1]
    assert name.endswith(".jpg")
    assert len(name) > 20
    assert (tmp_path / name).is_file()
    assert not stale.exists()
    assert frame_label() == "首帧公网地址：可用"
    get_settings.cache_clear()


def test_publish_without_base_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAME_PUBLISH_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ASSET_BASE_URL", "")
    monkeypatch.setenv("CLOUDFLARED_METRICS_URL", "")
    get_settings.cache_clear()
    assert publish_keyframe(_png(), width=720, height=1280) is None
    assert list(Path(tmp_path).glob("*.jpg")) == []
    assert frame_label() == "首帧公网地址：不可用"
    get_settings.cache_clear()


def test_tunnel_payload_parses_quicktunnel_and_metrics():
    assert parse_tunnel_payload('{"url":"https://abc.trycloudflare.com"}') == "https://abc.trycloudflare.com"
    assert parse_tunnel_payload('{"result":{"hostname":"xyz.trycloudflare.com"}}') == "https://xyz.trycloudflare.com"
    text = 'cloudflared_tunnel_user_hostnames{userHostname="https://metric.trycloudflare.com"} 1'
    assert parse_tunnel_payload(text) == "https://metric.trycloudflare.com"


def test_discover_tunnel_uses_metrics_without_calling_a_relay(monkeypatch):
    monkeypatch.setenv("CLOUDFLARED_METRICS_URL", "http://cloudflared:2000/quicktunnel")
    monkeypatch.setenv("PUBLIC_ASSET_BASE_URL", "")
    get_settings.cache_clear()
    from app.pipeline import frames

    frames._discovered["url"] = ""
    frames._discovered["at"] = 0

    def fake_get(url, timeout=2):
        class Response:
            status_code = 200
            text = '{"url":"https://live.trycloudflare.com"}' if url.endswith("/quicktunnel") else "no"

        return Response()

    monkeypatch.setattr(frames.httpx, "get", fake_get)
    assert discover_tunnel() == "https://live.trycloudflare.com"
    get_settings.cache_clear()
    frames._discovered["url"] = ""
