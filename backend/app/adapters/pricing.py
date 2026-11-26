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


def estimate_lines(provider_by_category: dict[str, str], units: dict) -> list[dict]:
    """One row per stage so the pre-run quote can be read before anyone clicks run."""
    providers = provider_by_category
    rows = [
        (
            "编剧",
            "llm",
            Usage(
                input_tokens=units["llm_input_tokens"] * units["llm_calls"],
                output_tokens=units["llm_output_tokens"] * units["llm_calls"],
            ),
            f"{units['llm_calls']} 次",
        ),
        ("定妆与分镜", "image", Usage(images=units["images"]), f"{units['images']} 张"),
        ("视频", "video", Usage(video_seconds=units["video_seconds"]), f"{units['video_seconds']:.1f} 秒"),
        ("配音", "tts", Usage(audio_chars=units["tts_chars"]), f"{units['tts_chars']} 字"),
        ("口型", "lipsync", Usage(lipsync_seconds=units["lipsync_seconds"]), f"{units['lipsync_seconds']:.1f} 秒"),
        ("配乐", "music", Usage(video_seconds=units["music_seconds"]), f"{units['music_seconds']:.1f} 秒"),
    ]
    lines = []
    for stage, category, usage, detail in rows:
        provider = providers.get(category, "mock")
        lines.append(
            {
                "stage": stage,
                "category": category,
                "provider": provider,
                "detail": detail,
                "usd": round(cost_of(category, provider, usage), 6),
            }
        )
    return lines


def estimate_usd(provider_by_category: dict[str, str], units: dict) -> float:
    return round(sum(line["usd"] for line in estimate_lines(provider_by_category, units)), 6)


def providers_of(provider_config: dict | None) -> dict[str, str]:
    selected = {}
    for category in ("llm", "image", "video", "tts", "lipsync", "music"):
        selected[category] = ((provider_config or {}).get(category) or {}).get("provider") or "mock"
    return selected


def forecast_from_counts(
    *,
    provider_config: dict | None,
    characters: int,
    locations: int,
    shots: int,
    dialogue_chars: int,
    video_seconds: float,
    preliminary: bool,
    assumption: str,
) -> dict:
    units = estimate_units(
        characters=characters,
        locations=locations,
        shots=shots,
        dialogue_chars=dialogue_chars,
        video_seconds=video_seconds,
    )
    selected = providers_of(provider_config)
    lines = estimate_lines(selected, units)
    total = round(sum(line["usd"] for line in lines), 6)
    video = round(sum(line["usd"] for line in lines if line["stage"] == "视频"), 6)
    return {
        "lines": lines,
        "total_usd": total,
        "final_usd": total,
        "draft_usd": round(total - video * 0.55, 6),
        "reference_usd": estimate_usd(REFERENCE_STACK, units),
        "preliminary": preliminary,
        "assumption": assumption,
        "units": {
            "characters": characters,
            "locations": locations,
            "shots": shots,
            "dialogue_chars": dialogue_chars,
            "video_seconds": video_seconds,
        },
    }


def forecast_novel(provider_config: dict | None, settings: dict | None, char_count: int) -> dict:
    """Quote a novel that has not been shot-listed yet.

    Shot count is characters/90, capped by the project's episode and shot limits.
    """
    settings = settings or {}
    cap = max(1, int(settings.get("max_episodes") or 2)) * max(1, int(settings.get("max_shots_per_episode") or 6))
    shots = max(1, min(cap, round((char_count or 400) / 90) or 4))
    return forecast_from_counts(
        provider_config=provider_config,
        characters=2,
        locations=1,
        shots=shots,
        dialogue_chars=min(char_count or shots * 12, shots * 24),
        video_seconds=shots * 2.5,
        preliminary=True,
        assumption=f"按约 {shots} 个镜头、每镜 2.5 秒估算，开拍后会按真实剧本重算。",
    )
