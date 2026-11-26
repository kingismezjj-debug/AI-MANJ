"""new-api video relay: paths, keyframe, poll, and model-id check. No live calls."""

import json

import httpx
import pytest

from app.adapters.capabilities import caps_for, clamp_video_request
from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.video.openai_compatible import OpenAICompatibleVideo
from app.settings_store import describe_model_match


def _adapter(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    video = OpenAICompatibleVideo(client=client, poll_interval=0, poll_timeout=5, **kwargs)
    video.api_key = "sk-test"
    video.base_url = "https://relay.example/v1"
    return video


def _run(video, model="MiniMax-H3", duration=4, width=720, height=1280):
    return video.image_to_video(
        prompt="苏桐回头",
        image=b"\x89PNG-keyframe",
        reference_images=[b"ref-a"],
        duration_sec=duration,
        seed=3,
        aspect_ratio="9:16",
        width=width,
        height=height,
        model=model,
    )


def test_new_api_create_poll_and_download():
    posts = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            body = json.loads(request.content)
            assert body["model"] == "MiniMax-H3"
            assert body["image"].startswith("data:image/png;base64,")
            assert body["images"][0].startswith("data:image/png;base64,")
            assert body["duration"] == 6
            assert body["resolution"] == "1080P"
            assert body["metadata"]["resolution"] == "1080P"
            return httpx.Response(200, json={"task_id": "task-1", "status": "queued"})
        if request.url.path == "/v1/video/generations/task-1":
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"task_id": "task-1", "status": "in_progress"})
            return httpx.Response(200, json={"status": "succeeded", "url": "https://cdn.example/clip.mp4"})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"mp4-bytes")
        return httpx.Response(404, json={"error": "Invalid URL"})

    result = _run(_adapter(handler))
    assert posts == ["/v1/video/generations"]
    assert result.data == b"mp4-bytes"
    assert result.model == "MiniMax-H3"
    assert result.duration_sec == 6
    assert result.provider == "openai_compatible"


def test_task_id_and_url_variants():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"task_id": "nested"}})
        if request.url.path.endswith("/nested"):
            return httpx.Response(200, json={"status": "completed", "data": [{"url": "https://cdn.example/nested.mp4"}]})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"nested-mp4")
        return httpx.Response(404, text="no")

    result = _run(_adapter(handler), model="kling-v2-6", duration=5, width=640, height=360)
    assert result.data == b"nested-mp4"
    assert result.model == "kling-v2-6"
    assert result.duration_sec == 5


def test_404_falls_through_to_openai_videos_path():
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            if request.url.path != "/v1/videos":
                return httpx.Response(404, text="Invalid URL")
            return httpx.Response(200, json={"id": "sora-9"})
        if request.url.path == "/v1/videos/sora-9":
            return httpx.Response(200, json={"status": "succeeded", "video_url": "https://cdn.example/sora.mp4"})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"sora-mp4")
        return httpx.Response(404, text="Invalid URL")

    result = _run(_adapter(handler), model="sora-2", duration=4, width=640, height=360)
    assert posts == ["/v1/video/generations", "/v1/videos/generations", "/v1/videos"]
    assert result.data == b"sora-mp4"


def test_client_error_does_not_try_another_create():
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(400, json={"error": {"message": "model_not_found"}})
        return httpx.Response(500, text="should not poll")

    with pytest.raises(ProviderResponseError, match="请点重试"):
        _run(_adapter(handler))
    assert posts == ["/v1/video/generations"]


def test_failure_status_is_visible():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "bad"})
        return httpx.Response(200, json={"status": "failure", "error": {"message": "余额不足"}})

    with pytest.raises(ProviderResponseError, match="余额不足"):
        _run(_adapter(handler), model="veo3.1-fast", duration=4, width=640, height=360)


def test_minimax_durations_round_up():
    caps = caps_for("openai_compatible", "MiniMax-Hailuo-2.3")
    assert caps["allowed_durations"] == [6, 10]
    assert caps["resolutions"] == ["768P", "1080P"]
    short = clamp_video_request("openai_compatible", references=[], last_image=None, duration_sec=4, merged=False, model="MiniMax-H3")
    mid = clamp_video_request("openai_compatible", references=[], last_image=None, duration_sec=7, merged=False, model="MiniMax-Hailuo-2.3")
    exact = clamp_video_request("openai_compatible", references=[], last_image=None, duration_sec=6, merged=False, model="MiniMax-H3")
    assert short["duration_sec"] == 6
    assert mid["duration_sec"] == 10
    assert exact["duration_sec"] == 6
    assert "6" in short["notice"]


def test_probe_rejects_wrong_model_case():
    names = ["MiniMax-H3", "MiniMax-Hailuo-2.3", "kling-v2-6", "seedance-2", "veo3.1-fast"]
    assert "未发起视频生成" in describe_model_match("MiniMax-H3", names)
    with pytest.raises(RuntimeError, match="请改成 MiniMax-H3"):
        describe_model_match("minimaxh3", names)
    with pytest.raises(RuntimeError, match="kling-v2-6"):
        describe_model_match("no-such-model", names)


def test_real_video_error_is_not_replaced_by_mock(db, monkeypatch):
    from app.adapters.router import ModelRouter
    from app.db.models import Project

    project = Project(title="视频", status="draft", provider_config={}, settings={"fallback_to_mock": True})
    db.add(project)
    db.commit()
    monkeypatch.setattr(
        "app.adapters.router.route_chain",
        lambda project, category, db=None: [
            {
                "provider": "openai_compatible",
                "model": "MiniMax-H3",
                "api_key": "sk-test",
                "base_url": "https://relay.example/v1",
                "fallbacks": [],
                "params": {},
            },
            {"provider": "mock", "model": "mock-video", "fallbacks": [], "params": {}},
        ],
    )

    def boom(*_args, **_kwargs):
        raise ProviderResponseError("视频提交失败：400 model_not_found。请点重试。")

    called = {"mock": 0}

    def fake_mock(*_args, **_kwargs):
        called["mock"] += 1
        raise AssertionError("mock video should not run")

    monkeypatch.setattr("app.adapters.video.openai_compatible.OpenAICompatibleVideo.image_to_video", boom)
    monkeypatch.setattr("app.adapters.video.mock.MockVideo.image_to_video", fake_mock)
    with pytest.raises(ProviderResponseError, match="model_not_found"):
        ModelRouter(db, project).video(
            prompt="x",
            image=b"png",
            reference_images=[],
            duration_sec=4,
            seed=1,
            aspect_ratio="9:16",
            width=720,
            height=1280,
        )
    assert called["mock"] == 0


def test_missing_video_key_still_names_the_problem():
    video = OpenAICompatibleVideo(client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))))
    video.api_key = ""
    with pytest.raises(ProviderNotConfigured, match="API Key"):
        _run(video)
