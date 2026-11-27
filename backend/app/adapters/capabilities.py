"""What each video adapter can actually do.

Stages pass every reference they have. The adapter that runs the call clamps
to its own limits, so a generic HTTP endpoint or Kling does not pretend to
be Seedance.
"""

DEFAULT_CAPS = {
    "max_reference_images": 1,
    "first_last_frame": False,
    "multi_shot": False,
    "max_duration_sec": 12,
    "merged_max_duration_sec": 12,
    "default_multi_reference": False,
}

CAPABILITIES = {
    "seedance": {
        "max_reference_images": 9,
        "first_last_frame": True,
        "multi_shot": True,
        "max_duration_sec": 15,
        "merged_max_duration_sec": 30,
        "default_multi_reference": True,
    },
    "kling": {
        "max_reference_images": 1,
        "first_last_frame": False,
        "multi_shot": False,
        "max_duration_sec": 10,
        "merged_max_duration_sec": 10,
        "default_multi_reference": False,
    },
    "http_poll": {
        "max_reference_images": 1,
        "first_last_frame": False,
        "multi_shot": False,
        "max_duration_sec": 10,
        "merged_max_duration_sec": 10,
        "default_multi_reference": False,
    },
    "openai_compatible": {
        "max_reference_images": 3,
        "first_last_frame": False,
        "multi_shot": False,
        "max_duration_sec": 10,
        "merged_max_duration_sec": 10,
        "default_multi_reference": False,
    },
    "minimax": {
        "max_reference_images": 3,
        "first_last_frame": False,
        "multi_shot": False,
        "max_duration_sec": 10,
        "merged_max_duration_sec": 10,
        "default_multi_reference": False,
        "allowed_durations": [6, 10],
        "resolutions": ["768P", "1080P"],
    },
    "mock": {
        "max_reference_images": 8,
        "first_last_frame": True,
        "multi_shot": True,
        "max_duration_sec": 30,
        "merged_max_duration_sec": 30,
        "default_multi_reference": True,
    },
}


def is_minimax(model: str | None) -> bool:
    return (model or "").lower().startswith("minimax")


def hailuo_resolution(width: int, height: int) -> str:
    return "1080P" if max(int(width or 0), int(height or 0)) >= 1000 else "768P"


def caps_for(provider: str, model: str | None = None) -> dict:
    if provider == "openai_compatible" and is_minimax(model):
        return dict(CAPABILITIES["minimax"])
    return dict(CAPABILITIES.get(provider, DEFAULT_CAPS))


def clamp_video_request(
    provider: str,
    *,
    references: list,
    last_image,
    duration_sec: float,
    merged: bool,
    model: str | None = None,
) -> dict:
    caps = caps_for(provider, model)
    limit = int(caps["max_reference_images"])
    kept = list(references or [])[:limit]
    notices = []
    dropped = len(references or []) - len(kept)
    if dropped > 0:
        notices.append(f"{provider} 最多 {limit} 张参考，已去掉 {dropped} 张")
    last = last_image if caps["first_last_frame"] else None
    if last_image and last is None:
        notices.append(f"{provider} 不支持首尾帧，已忽略尾帧")
    allowed = [int(item) for item in (caps.get("allowed_durations") or [])]
    raw = float(duration_sec or 4)
    if allowed:
        duration = float(next((item for item in allowed if raw <= item), allowed[-1]))
        if duration != raw:
            notices.append(f"时长已调到 {int(duration)} 秒（该模型支持 {'/'.join(str(item) for item in allowed)} 秒）")
    else:
        ceiling = float(caps["merged_max_duration_sec"] if merged else caps["max_duration_sec"])
        duration = max(2.0, min(ceiling, raw))
        if raw > ceiling:
            notices.append(f"时长已收到 {int(ceiling)} 秒")
    return {
        "references": kept,
        "last_image": last,
        "duration_sec": duration,
        "notice": "；".join(notices),
        "multi_reference": bool(caps["default_multi_reference"]) and bool(kept),
        "resolutions": list(caps.get("resolutions") or []),
    }
