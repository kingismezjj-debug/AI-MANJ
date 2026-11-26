"""Provider registry.

Adding a vendor is a builder function plus a (category, provider) entry.
Stages never import concrete clients; they call ModelRouter.
"""

from app.adapters.errors import ProviderNotConfigured
from app.adapters.image.mock import MockImage
from app.adapters.image.openai_compatible import OpenAICompatibleImage
from app.adapters.image.seedream import SeedreamImage
from app.adapters.lipsync.kling import KlingLipSync
from app.adapters.lipsync.mock import MockLipSync
from app.adapters.llm.mock import MockLLM
from app.adapters.llm.openai_compatible import OpenAICompatibleLLM
from app.adapters.music.mock import MockMusic
from app.adapters.tts.elevenlabs import ElevenLabsTTS
from app.adapters.tts.minimax import MiniMaxTTS
from app.adapters.tts.mock import MockTTS
from app.adapters.video.http_poll import HttpPollVideo
from app.adapters.video.openai_compatible import OpenAICompatibleVideo
from app.adapters.video.kling import KlingVideo
from app.adapters.video.mock import MockVideo
from app.adapters.video.seedance import SeedanceVideo


def default_provider_config() -> dict:
    def route(provider: str, model: str) -> dict:
        return {"provider": provider, "model": model, "fallbacks": [], "params": {}}

    return {
        "llm": route("mock", "mock-screenwriter"),
        "image": route("mock", "mock-image"),
        "video": route("mock", "mock-video"),
        "tts": route("mock", "mock-tts"),
        "lipsync": route("mock", "mock-lipsync"),
        "music": route("mock", "mock-music"),
    }


def preset_config(name: str) -> dict:
    if name == "cn_stack":
        return {
            "llm": {"provider": "openai_compatible", "model": "", "fallbacks": ["mock"], "params": {}},
            "image": {"provider": "seedream", "model": "", "fallbacks": ["mock"], "params": {}},
            "video": {"provider": "seedance", "model": "", "fallbacks": ["mock"], "params": {}},
            "tts": {"provider": "minimax", "model": "", "fallbacks": ["mock"], "params": {}},
            "lipsync": {"provider": "kling", "model": "", "fallbacks": ["mock"], "params": {}},
            "music": {"provider": "mock", "model": "mock-music", "fallbacks": [], "params": {}},
        }
    if name == "global_stack":
        return {
            "llm": {"provider": "openai_compatible", "model": "", "fallbacks": ["mock"], "params": {}},
            "image": {"provider": "openai_compatible", "model": "", "fallbacks": ["mock"], "params": {}},
            "video": {"provider": "kling", "model": "", "fallbacks": ["mock"], "params": {}},
            "tts": {"provider": "elevenlabs", "model": "", "fallbacks": ["mock"], "params": {}},
            "lipsync": {"provider": "kling", "model": "", "fallbacks": ["mock"], "params": {}},
            "music": {"provider": "mock", "model": "mock-music", "fallbacks": [], "params": {}},
        }
    return default_provider_config()


def default_settings(*, require_bible_approval: bool = False, require_script_approval: bool = False) -> dict:
    return {
        "require_bible_approval": require_bible_approval,
        "require_script_approval": require_script_approval,
        "use_global_models": True,
        "script_approved": False,
        "max_qc_retries": 1,
        "max_episodes": 2,
        "max_shots_per_episode": 6,
        "episode_count": 60,
        "produce_first": 2,
        "shots_per_episode": 6,
        "draft_mode": False,
        "merge_shots": False,
        "require_grid_confirm": False,
        "fallback_to_mock": True,
        "show_ai_label": True,
    }


BUILDERS = {
    ("llm", "mock"): lambda: MockLLM(),
    ("llm", "openai_compatible"): lambda: OpenAICompatibleLLM(),
    ("image", "mock"): lambda: MockImage(),
    ("image", "seedream"): lambda: SeedreamImage(),
    ("image", "openai_compatible"): lambda: OpenAICompatibleImage(),
    ("video", "mock"): lambda: MockVideo(),
    ("video", "seedance"): lambda: SeedanceVideo(),
    ("video", "kling"): lambda: KlingVideo(),
    ("video", "http_poll"): lambda: HttpPollVideo(),
    ("video", "openai_compatible"): lambda: OpenAICompatibleVideo(),
    ("tts", "mock"): lambda: MockTTS(),
    ("tts", "minimax"): lambda: MiniMaxTTS(),
    ("tts", "elevenlabs"): lambda: ElevenLabsTTS(),
    ("lipsync", "mock"): lambda: MockLipSync(),
    ("lipsync", "kling"): lambda: KlingLipSync(),
    ("music", "mock"): lambda: MockMusic(),
}


def build_adapter(category: str, route: dict):
    provider = (route or {}).get("provider") or "mock"
    builder = BUILDERS.get((category, provider))
    if builder is None:
        raise ProviderNotConfigured(f"unknown provider {category}/{provider}")
    adapter = builder()
    if hasattr(adapter, "apply_route"):
        adapter.apply_route(route or {})
    if hasattr(adapter, "configure"):
        adapter.configure((route or {}).get("params") or {})
    return adapter


def route_chain(project, category: str, db=None) -> list[dict]:
    if db is not None:
        from app.settings_store import effective_slot

        configured = effective_slot(db, project, category)
    else:
        configured = (project.provider_config or {}).get(category) or {"provider": "mock", "model": f"mock-{category}"}
    chain = [configured]
    for name in configured.get("fallbacks") or []:
        chain.append({"provider": name, "model": configured.get("model") if name == configured.get("provider") else "", "fallbacks": [], "params": {}})
    settings = project.settings or {}
    if settings.get("fallback_to_mock", True) and all(item.get("provider") != "mock" for item in chain):
        chain.append({"provider": "mock", "model": f"mock-{category}", "fallbacks": [], "params": {}})
    return chain
