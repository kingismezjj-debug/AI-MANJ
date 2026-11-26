"""Unit prices used for metering.

Figures are planning estimates in USD, not invoices. Adjust them to match the
relay or vendor contract you actually bill against. Mock providers cost nothing
so the pipeline can be demonstrated without keys.
"""

from app.adapters.types import Usage

UNIT_PRICES: dict[str, dict[str, float]] = {
    "mock": {
        "input_per_1k": 0.0,
        "output_per_1k": 0.0,
        "image": 0.0,
        "video_sec": 0.0,
        "tts_1k_chars": 0.0,
        "lipsync_sec": 0.0,
        "music_sec": 0.0,
    },
    "openai_compatible": {
        "input_per_1k": 0.00014,
        "output_per_1k": 0.00028,
        "image": 0.04,
    },
    "seedream": {"image": 0.03},
    "seedance": {"video_sec": 0.05},
    "kling": {"video_sec": 0.07, "lipsync_sec": 0.04},
    "http_poll": {"video_sec": 0.05},
    "minimax": {"tts_1k_chars": 0.02},
    "elevenlabs": {"tts_1k_chars": 0.15},
}

# What a typical Chinese commercial stack would have cost for the quote line.
REFERENCE_STACK = {
    "llm": "openai_compatible",
    "image": "seedream",
    "video": "seedance",
    "tts": "minimax",
    "lipsync": "kling",
    "music": "mock",
}


def cost_of(category: str, provider: str, usage: Usage) -> float:
    prices = UNIT_PRICES.get(provider, {})
    if category == "llm":
        return (usage.input_tokens / 1000) * prices.get("input_per_1k", 0) + (
            usage.output_tokens / 1000
        ) * prices.get("output_per_1k", 0)
    if category == "image":
        return usage.images * prices.get("image", 0)
    if category == "video":
        return usage.video_seconds * prices.get("video_sec", 0)
    if category == "tts":
        return (usage.audio_chars / 1000) * prices.get("tts_1k_chars", 0)
    if category == "lipsync":
        return usage.lipsync_seconds * prices.get("lipsync_sec", 0)
    if category == "music":
        return usage.video_seconds * prices.get("music_sec", 0)
    return 0.0


def estimate_units(*, characters: int, locations: int, shots: int, dialogue_chars: int, video_seconds: float) -> dict:
    """Planned units once a screenplay exists. LLM size is a fixed planning allowance."""
    return {
        "llm_input_tokens": 1800,
        "llm_output_tokens": 1200,
        "llm_calls": 2,
        "images": characters + locations + shots,
        "video_seconds": video_seconds,
        "tts_chars": dialogue_chars * 2,
        "lipsync_seconds": video_seconds,
        "music_seconds": video_seconds,
    }


def estimate_usd(provider_by_category: dict[str, str], units: dict) -> float:
    llm = cost_of(
        "llm",
        provider_by_category.get("llm", "mock"),
        Usage(input_tokens=units["llm_input_tokens"] * units["llm_calls"], output_tokens=units["llm_output_tokens"] * units["llm_calls"]),
    )
    image = cost_of("image", provider_by_category.get("image", "mock"), Usage(images=units["images"]))
    video = cost_of("video", provider_by_category.get("video", "mock"), Usage(video_seconds=units["video_seconds"]))
    tts = cost_of("tts", provider_by_category.get("tts", "mock"), Usage(audio_chars=units["tts_chars"]))
    lipsync = cost_of(
        "lipsync", provider_by_category.get("lipsync", "mock"), Usage(lipsync_seconds=units["lipsync_seconds"])
    )
    music = cost_of("music", provider_by_category.get("music", "mock"), Usage(video_seconds=units["music_seconds"]))
    return round(llm + image + video + tts + lipsync + music, 6)
