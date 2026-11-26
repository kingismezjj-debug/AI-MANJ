"""Provider catalog shown in the UI and described in the README."""

from app.config import get_settings

PROVIDERS = [
    {"category": "llm", "provider": "mock", "label": "本地模拟", "env": [], "hint": "不调用外网"},
    {"category": "llm", "provider": "openai_compatible", "label": "OpenAI 兼容", "env": ["OPENAI_API_KEY"], "hint": "DeepSeek 或任意中转，改 OPENAI_BASE_URL"},
    {"category": "image", "provider": "mock", "label": "本地模拟", "env": [], "hint": "本地绘制定妆和分镜"},
    {"category": "image", "provider": "seedream", "label": "Seedream（方舟）", "env": ["ARK_API_KEY"], "hint": "火山方舟图片生成"},
    {"category": "image", "provider": "openai_compatible", "label": "OpenAI 兼容图片", "env": ["OPENAI_API_KEY"], "hint": "POST /images/generations"},
    {"category": "video", "provider": "mock", "label": "本地模拟", "env": [], "hint": "ffmpeg 静帧成片"},
    {"category": "video", "provider": "seedance", "label": "Seedance（方舟）", "env": ["ARK_API_KEY"], "hint": "图生视频，首帧"},
    {"category": "video", "provider": "kling", "label": "可灵 Kling", "env": ["KLING_API_KEY"], "hint": "也可用 KLING_ACCESS_KEY + KLING_SECRET_KEY"},
    {"category": "video", "provider": "http_poll", "label": "通用 HTTP 轮询", "env": ["VIDEO_HTTP_API_KEY"], "hint": "在 params 里填 create_url / poll_url"},
    {"category": "tts", "provider": "mock", "label": "本地模拟", "env": [], "hint": "合成占位音色"},
    {"category": "tts", "provider": "minimax", "label": "MiniMax Speech", "env": ["MINIMAX_API_KEY"], "hint": "T2A v2"},
    {"category": "tts", "provider": "elevenlabs", "label": "ElevenLabs", "env": ["ELEVENLABS_API_KEY"], "hint": "multilingual v2"},
    {"category": "lipsync", "provider": "mock", "label": "本地模拟", "env": [], "hint": "把成片音轨贴上视频"},
    {"category": "lipsync", "provider": "kling", "label": "可灵口型", "env": ["KLING_API_KEY"], "hint": "需要公网可访问的视频 URL"},
    {"category": "music", "provider": "mock", "label": "本地模拟", "env": [], "hint": "简单配乐底床"},
]

PRESETS = [
    {"id": "mock", "label": "本地模拟（无需密钥）", "detail": "整条流水线在本机跑完"},
    {"id": "cn_stack", "label": "国内商用组合", "detail": "兼容 LLM + Seedream + Seedance + MiniMax + 可灵口型"},
    {"id": "global_stack", "label": "海外组合", "detail": "兼容 LLM / 图片 + Kling 视频与口型 + ElevenLabs"},
]

STAGE_LABELS = {
    "ingest": "读入",
    "screenwrite": "编剧",
    "character_bible": "定妆",
    "location_bible": "场景",
    "character_lock": "锁定",
    "storyboard": "分镜",
    "tts_temp": "预配",
    "animatic": "预演",
    "video": "视频",
    "voice": "配音",
    "lipsync": "口型",
    "shot_qc": "质检",
    "music": "配乐",
    "assemble_episode": "单集",
    "assemble_final": "成片",
    "qc_final": "终检",
}


def _configured(env_names: list[str]) -> bool:
    if not env_names:
        return True
    settings = get_settings()
    for name in env_names:
        if name == "KLING_API_KEY":
            if settings.kling_api_key or (settings.kling_access_key and settings.kling_secret_key):
                continue
            return False
        value = getattr(settings, name.lower(), None)
        if value is None:
            import os

            value = os.environ.get(name, "")
        if not value:
            return False
    return True


def public_catalog() -> dict:
    providers = []
    for item in PROVIDERS:
        providers.append({**item, "configured": _configured(item["env"])})
    return {"providers": providers, "presets": PRESETS, "stage_labels": STAGE_LABELS}
