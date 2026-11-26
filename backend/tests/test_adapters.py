import httpx
import jwt

from app.adapters.errors import TransientProviderError
from app.adapters.http import send
from app.adapters.image.seedream import seedream_size
from app.adapters.kling_auth import kling_headers
from app.adapters.llm.mock import screenplay_from_novel
from app.adapters.pricing import REFERENCE_STACK, estimate_units, estimate_usd
from app.adapters.tts.elevenlabs import ElevenLabsTTS
from app.adapters.tts.minimax import MiniMaxTTS
from app.adapters.video.http_poll import HttpPollVideo
from app.adapters.video.kling import KlingVideo
from app.adapters.video.seedance import SeedanceVideo
from app.config import get_settings
from app.pipeline.schema import parse_screenplay


class _Settings:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_mock_screenplay_matches_schema():
    novel = open(get_settings().sample_path(), encoding="utf-8").read()
    payload = screenplay_from_novel(novel, max_shots=6)
    screenplay = parse_screenplay(__import__("json").dumps(payload, ensure_ascii=False))
    assert screenplay.episodes[0].scenes[0].shots
    assert any(shot.dialogue.startswith("三年了") for shot in screenplay.episodes[0].scenes[0].shots)
    names = {item.name for item in screenplay.characters}
    assert {"林夏", "陈默"} <= names


def test_seedream_size_rules():
    assert seedream_size("doubao-seedream-4-0-250828", 720, 1280) == "720x1280"
    assert seedream_size("doubao-seedream-4-5-251128", 720, 1280) == "1600x2848"


def test_kling_jwt_and_api_key(monkeypatch):
    headers = kling_headers(_Settings(kling_api_key="live-key", kling_access_key="", kling_secret_key=""))
    assert headers["Authorization"] == "Bearer live-key"
    signed = kling_headers(_Settings(kling_api_key="", kling_access_key="ak-1", kling_secret_key="sk-1"))
    token = signed["Authorization"].split()[1]
    payload = jwt.decode(token, "sk-1", algorithms=["HS256"])
    assert payload["iss"] == "ak-1"


def test_seedance_polls_and_downloads(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "ark-test")
    get_settings.cache_clear()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = __import__("json").loads(request.content)
            assert body["ratio"] == "adaptive"
            assert body["content"][1]["role"] == "first_frame"
            assert body["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            return httpx.Response(200, json={"id": "task-1"})
        if request.url.path.endswith("/tasks/task-1"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"id": "task-1", "status": "running"})
            return httpx.Response(
                200,
                json={"id": "task-1", "status": "succeeded", "duration": 5, "content": {"video_url": "https://cdn.example/v.mp4"}},
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"fake-mp4")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SeedanceVideo(client=client, poll_interval=0, poll_timeout=5)
    result = adapter.image_to_video(
        prompt="雨夜",
        image=b"\x89PNG",
        reference_images=[b"ref"],
        duration_sec=3,
        seed=7,
        aspect_ratio="9:16",
        width=720,
        height=1280,
        model="doubao-seedance-2-0-260128",
    )
    assert result.data == b"fake-mp4"
    assert result.usage.video_seconds == 5
    assert calls["n"] == 2
    get_settings.cache_clear()


def test_kling_image2video_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = __import__("json").loads(request.content)
            assert body["model_name"] == "kling-v2-6"
            assert body["duration"] == "5"
            assert body["aspect_ratio"] == "9:16"
            assert not body["image"].startswith("data:")
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "k1", "task_status": "submitted"}})
        if request.url.path.endswith("/k1"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"task_status": "succeed", "task_result": {"videos": [{"url": "https://cdn.example/k.mp4", "duration": "5"}]}}},
            )
        return httpx.Response(200, content=b"kling-bytes")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = KlingVideo(client=client, poll_interval=0, poll_timeout=5)
    adapter.settings.kling_api_key = "live-key"
    result = adapter.image_to_video(
        prompt="走近",
        image=b"frame",
        reference_images=[],
        duration_sec=2.5,
        seed=1,
        aspect_ratio="9:16",
        width=720,
        height=1280,
        model=None,
    )
    assert result.data == b"kling-bytes"
    assert result.provider == "kling"


def test_http_poll_keeps_duration_type():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = __import__("json").loads(request.content)
            return httpx.Response(200, json={"data": {"task_id": "h1"}})
        if str(request.url).endswith("/h1"):
            return httpx.Response(200, json={"data": {"status": "succeeded", "output": {"url": "https://cdn.example/h.mp4"}}})
        return httpx.Response(200, content=b"http-mp4")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = HttpPollVideo(client=client)
    adapter.configure(
        {
            "create_url": "https://vendor.example/v1/jobs",
            "poll_url": "https://vendor.example/v1/jobs/{task_id}",
            "task_id_path": "data.task_id",
            "status_path": "data.status",
            "video_url_path": "data.output.url",
            "poll_interval": 0,
            "poll_timeout": 5,
            "body": {"model": "{model}", "prompt": "shot {prompt}", "duration": "{duration}"},
        }
    )
    result = adapter.image_to_video(
        prompt="推门",
        image=b"img",
        reference_images=[],
        duration_sec=4.2,
        seed=3,
        aspect_ratio="9:16",
        width=720,
        height=1280,
        model="vendor-x",
    )
    assert seen["body"]["duration"] == 4
    assert seen["body"]["prompt"] == "shot 推门"
    assert result.data == b"http-mp4"


def test_minimax_and_elevenlabs_shapes(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "mini-key")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    get_settings.cache_clear()

    def mini(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        assert body["voice_setting"]["voice_id"] == "male-qn-qingse"
        assert body["output_format"] == "hex"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"base_resp": {"status_code": 0}, "data": {"audio": b"ID3mini".hex()}})

    def eleven(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "el-key"
        assert request.url.path.endswith("/voice-1")
        return httpx.Response(200, content=b"mpeg-bytes")

    mini_result = MiniMaxTTS(client=httpx.Client(transport=httpx.MockTransport(mini))).synthesize(
        text="你好", voice="male", speed=1, model=None
    )
    assert mini_result.data == b"ID3mini"
    eleven_result = ElevenLabsTTS(client=httpx.Client(transport=httpx.MockTransport(eleven))).synthesize(
        text="你好", voice="voice-1", speed=1, model=None
    )
    assert eleven_result.data == b"mpeg-bytes"
    assert eleven_result.usage.audio_chars == 2
    get_settings.cache_clear()


def test_get_retries_server_errors():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = send(client, "GET", "https://example.test/poll", idempotent=True)
    assert response.json()["ok"] is True
    assert state["n"] == 2


def test_post_does_not_retry_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        send(client, "POST", "https://example.test/create", idempotent=False, json={})
    except Exception as exc:
        assert not isinstance(exc, TransientProviderError)
        assert "500" in str(exc)
    else:
        raise AssertionError("expected provider error")


def test_router_falls_back_to_mock(db):
    from app.adapters.registry import default_provider_config, default_settings
    from app.adapters.router import ModelRouter
    from app.db.models import ModelCall, Project

    config = default_provider_config()
    config["llm"] = {"provider": "openai_compatible", "model": "gpt-test", "fallbacks": [], "params": {}}
    project = Project(title="回退", provider_config=config, settings=default_settings())
    db.add(project)
    db.commit()
    get_settings.cache_clear()
    result = ModelRouter(db, project).llm(
        [{"role": "user", "content": "TASK:ANALYSIS\n<<<NOVEL\n雨下了一夜。\n>>>"}],
        json_mode=True,
    )
    assert "summary" in result.text
    calls = db.query(ModelCall).all()
    assert any(call.provider == "openai_compatible" and call.status == "failed" for call in calls)
    assert any(call.provider == "mock" and call.status == "succeeded" for call in calls)


def test_reference_quote_is_positive():
    units = estimate_units(characters=2, locations=1, shots=4, dialogue_chars=40, video_seconds=10)
    assert estimate_usd({"llm": "mock", "image": "mock", "video": "mock", "tts": "mock", "lipsync": "mock", "music": "mock"}, units) == 0
    assert estimate_usd(REFERENCE_STACK, units) > 0.5
