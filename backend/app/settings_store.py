"""Studio model slots.

The three APIs the owner actually switches are the brain (LLM), text-to-image,
and video. TTS, lip-sync, and music stay secondary and mock unless changed.
Values saved here override environment variables on the next call. No restart.
"""

import json
import time

import httpx

from app.config import get_settings
from app.db.models import ApiSlot

CATEGORIES = ("llm", "image", "video", "tts", "lipsync", "music")
PRIMARY = ("llm", "image", "video")

DEFAULTS = {
    "llm": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "params": {},
    },
    "image": {"provider": "mock", "base_url": "", "model": "mock-image", "params": {}},
    "video": {"provider": "mock", "base_url": "", "model": "mock-video", "params": {}},
    "tts": {"provider": "mock", "base_url": "", "model": "mock-tts", "params": {}},
    "lipsync": {"provider": "mock", "base_url": "", "model": "mock-lipsync", "params": {}},
    "music": {"provider": "mock", "base_url": "", "model": "mock-music", "params": {}},
}

PROVIDER_OPTIONS = {
    "llm": ["mock", "openai_compatible"],
    "image": ["mock", "seedream", "openai_compatible"],
    "video": ["mock", "seedance", "kling", "http_poll", "openai_compatible"],
    "tts": ["mock", "minimax", "elevenlabs"],
    "lipsync": ["mock", "kling"],
    "music": ["mock"],
}


def mask_key(secret: str) -> str:
    secret = secret or ""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:3]}••••{secret[-4:]}"


def _env_key(category: str, provider: str) -> str:
    settings = get_settings()
    if provider == "openai_compatible":
        return settings.openai_api_key
    if provider in {"seedream", "seedance"}:
        return settings.ark_api_key
    if provider == "kling":
        return settings.kling_api_key
    if provider == "minimax":
        return settings.minimax_api_key
    if provider == "elevenlabs":
        return settings.elevenlabs_api_key
    if provider == "http_poll":
        return settings.video_http_api_key
    return ""


def _env_base(category: str, provider: str) -> str:
    settings = get_settings()
    if provider == "openai_compatible" and category == "llm":
        return settings.openai_base_url
    if provider == "openai_compatible":
        return settings.openai_base_url
    if provider in {"seedream", "seedance"}:
        return settings.ark_base_url
    if provider == "kling":
        return settings.kling_base_url
    if provider == "minimax":
        return settings.minimax_base_url
    if provider == "elevenlabs":
        return settings.elevenlabs_base_url
    return ""


def _env_model(category: str, provider: str) -> str:
    settings = get_settings()
    if category == "llm" and provider == "openai_compatible":
        return settings.openai_model
    if category == "image" and provider == "openai_compatible":
        return settings.openai_image_model
    if provider == "seedream":
        return settings.seedream_model
    if provider == "seedance":
        return settings.seedance_model
    if category == "video" and provider == "kling":
        return settings.kling_video_model
    if provider == "minimax":
        return settings.minimax_model
    if provider == "elevenlabs":
        return settings.elevenlabs_model
    return DEFAULTS[category]["model"]


def ensure_defaults(db, *, commit: bool = False) -> None:
    for category, default in DEFAULTS.items():
        row = (
            db.query(ApiSlot)
            .filter(ApiSlot.scope == "global", ApiSlot.project_id == "", ApiSlot.category == category)
            .one_or_none()
        )
        if row is None:
            db.add(
                ApiSlot(
                    scope="global",
                    project_id="",
                    category=category,
                    provider=default["provider"],
                    base_url=default["base_url"],
                    model=default["model"],
                    params=default["params"],
                    api_key="",
                    enabled=True,
                )
            )
    if commit:
        db.commit()
    else:
        db.flush()


def _row(db, scope: str, project_id: str, category: str) -> ApiSlot | None:
    return (
        db.query(ApiSlot)
        .filter(ApiSlot.scope == scope, ApiSlot.project_id == project_id, ApiSlot.category == category)
        .one_or_none()
    )


def public_slot(row: ApiSlot | None, category: str) -> dict:
    default = DEFAULTS[category]
    provider = row.provider if row else default["provider"]
    base_url = (row.base_url if row else "") or _env_base(category, provider) or default["base_url"]
    model = (row.model if row else "") or _env_model(category, provider)
    stored = row.api_key if row and row.api_key else ""
    env_key = _env_key(category, provider)
    key_present = bool(stored or env_key)
    return {
        "category": category,
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "params": (row.params if row else None) or {},
        "enabled": True if row is None else bool(row.enabled),
        "api_key_set": key_present,
        "api_key_mask": mask_key((stored or env_key).splitlines()[0].strip()) if (stored or env_key) else "",
        "api_key_masks": [mask_key(part.strip()) for part in (stored or env_key).replace(",", "\n").splitlines() if part.strip()],
        "api_key_count": len([part for part in (stored or env_key).replace(",", "\n").splitlines() if part.strip()]),
        "key_source": "database" if stored else ("env" if env_key else ""),
        "providers": PROVIDER_OPTIONS[category],
    }


def list_slots(db, project_id: str | None = None) -> dict:
    ensure_defaults(db, commit=True)
    global_slots = [public_slot(_row(db, "global", "", category), category) for category in CATEGORIES]
    overrides = []
    if project_id:
        overrides = []
        for category in CATEGORIES:
            row = _row(db, "project", project_id, category)
            if row is None:
                overrides.append({**public_slot(None, category), "enabled": False, "api_key_set": False, "api_key_mask": ""})
            else:
                overrides.append(public_slot(row, category))
    return {"slots": global_slots, "overrides": overrides}


def _keep_key(submitted: str, existing: str) -> str:
    submitted = (submitted or "").strip()
    if not submitted or "•" in submitted or submitted == mask_key(existing):
        return existing
    return submitted


def save_slots(db, slots: list[dict], *, project_id: str | None = None) -> dict:
    ensure_defaults(db, commit=True)
    scope = "project" if project_id else "global"
    owner = project_id or ""
    for item in slots:
        category = item.get("category")
        if category not in CATEGORIES:
            continue
        row = _row(db, scope, owner, category)
        if row is None:
            row = ApiSlot(scope=scope, project_id=owner, category=category)
            db.add(row)
        row.provider = item.get("provider") or "mock"
        row.base_url = (item.get("base_url") or "").strip()
        row.model = (item.get("model") or "").strip()
        params = item.get("params") or {}
        if isinstance(params, str):
            params = json.loads(params) if params.strip() else {}
        row.params = params
        row.enabled = bool(item.get("enabled", True)) if project_id else True
        row.api_key = _keep_key(item.get("api_key") or "", row.api_key or "")
    db.commit()
    return list_slots(db, project_id)


def _materialize(row: ApiSlot | None, category: str) -> dict:
    default = DEFAULTS[category]
    provider = (row.provider if row else None) or default["provider"]
    base_url = ((row.base_url if row else "") or _env_base(category, provider) or default["base_url"]).rstrip("/")
    model = (row.model if row else "") or _env_model(category, provider)
    stored = (row.api_key if row else "") or ""
    api_key = stored or _env_key(category, provider)
    params = dict((row.params if row else None) or {})
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "params": params,
        "fallbacks": [],
    }


def configured_slot(db, project, category: str) -> dict:
    ensure_defaults(db)
    override = _row(db, "project", project.id, category)
    if override is not None and override.enabled:
        return _materialize(override, category)
    if not (project.settings or {}).get("use_global_models", True):
        legacy = (project.provider_config or {}).get(category) or {}
        slot = _materialize(_row(db, "global", "", category), category)
        if legacy.get("provider"):
            slot["provider"] = legacy["provider"]
            if legacy.get("model"):
                slot["model"] = legacy["model"]
            slot["params"] = {**slot.get("params", {}), **(legacy.get("params") or {})}
            slot["api_key"] = slot["api_key"] or _env_key(category, slot["provider"])
            if not slot["base_url"]:
                slot["base_url"] = _env_base(category, slot["provider"])
        return slot
    return _materialize(_row(db, "global", "", category), category)


def effective_slot(db, project, category: str) -> dict:
    slot = configured_slot(db, project, category)
    if slot["provider"] != "mock" and not slot.get("api_key") and slot["provider"] != "http_poll":
        configured = f"{slot['provider']}/{slot.get('model') or ''}".strip("/")
        slot["_note"] = f"配置为 {configured}，密钥为空，本次改用本地模拟"
        slot["provider"] = "mock"
        slot["model"] = f"mock-{category}"
    elif slot.get("provider") == "http_poll" and not (slot.get("params") or {}).get("create_url"):
        slot["_note"] = "通用视频接口还没填 create_url，本次改用本地模拟"
        slot["provider"] = "mock"
        slot["model"] = "mock-video"
    return slot


def choose_models(names: list[str] | None) -> dict:
    names = names or []

    def pick(hints: tuple[str, ...], fallback: str) -> str:
        for name in names:
            lowered = name.lower()
            if any(hint in lowered for hint in hints):
                return name
        return fallback

    return {
        "llm": pick(("deepseek", "gpt", "chat", "reasoner", "claude"), "deepseek-chat"),
        "image": pick(("seedream", "image", "dall", "flux", "sd"), "seedream"),
        "video": pick(("seedance", "video", "kling", "sora"), "seedance"),
    }


def apply_relay(db, base_url: str, api_key: str, models: list[str] | None = None) -> dict:
    """One relay base URL and key fills the brain, image, and video slots."""
    base = normalize_openai_base(base_url)
    picked = choose_models(models)
    slots = [
        {"category": "llm", "provider": "openai_compatible", "base_url": base, "model": picked["llm"], "api_key": api_key, "params": {}, "enabled": True},
        {"category": "image", "provider": "openai_compatible", "base_url": base, "model": picked["image"], "api_key": api_key, "params": {}, "enabled": True},
        {"category": "video", "provider": "openai_compatible", "base_url": base, "model": picked["video"], "api_key": api_key, "params": {}, "enabled": True},
    ]
    return save_slots(db, slots)


def fetch_model_ids(base_url: str, api_key: str) -> list[str]:
    import httpx

    url = normalize_openai_base(base_url) + "/models"
    response = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=12)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") or payload.get("models") or []
    names = []
    for row in rows:
        if isinstance(row, str):
            names.append(row)
        elif isinstance(row, dict):
            names.append(str(row.get("id") or row.get("name") or ""))
    return [name for name in names if name]


def normalize_openai_base(url: str) -> str:
    url = (url or "").rstrip("/")
    if not url:
        return "https://api.deepseek.com/v1"
    if url.endswith("/v1"):
        return url
    return url + "/v1"


def probe_slot(db, form: dict) -> dict:
    """Run the smallest call that proves the slot is wired.

    Mock slots stay local. The brain slot sends one short chat completion.
    Image and video slots hit the configured base URL with the saved key and
    return the HTTP status. They do not start a billed generation.
    """
    category = form.get("category") or "llm"
    if category not in CATEGORIES:
        return {"ok": False, "latency_ms": 0, "error": "未知接口"}
    project_id = form.get("project_id") or ""
    scope = "project" if project_id else "global"
    existing = _row(db, scope, project_id, category) or _row(db, "global", "", category)
    submitted_key = form.get("api_key") or ""
    api_key = _keep_key(submitted_key, (existing.api_key if existing else "") or "")
    if not api_key:
        api_key = _env_key(category, form.get("provider") or (existing.provider if existing else "mock"))
    slot = {
        "provider": form.get("provider") or (existing.provider if existing else "mock"),
        "base_url": (form.get("base_url") or (existing.base_url if existing else "") or "").rstrip("/"),
        "model": form.get("model") or (existing.model if existing else ""),
        "api_key": api_key,
        "params": form.get("params") or {},
    }
    started = time.perf_counter()
    try:
        detail = _probe(category, slot)
        ok = True
        error = ""
    except Exception as exc:
        detail = ""
        ok = False
        error = str(exc)[:500]
    return {
        "ok": ok,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "detail": detail,
        "error": error,
        "provider": slot["provider"],
        "model": slot["model"],
    }


def _probe(category: str, slot: dict) -> str:
    provider = slot["provider"]
    if provider == "mock":
        if category == "llm":
            from app.adapters.llm.mock import MockLLM

            result = MockLLM().complete(
                messages=[{"role": "user", "content": "TASK:DIRECTOR\n<<<NOVEL\n测试连接\n>>>"}],
                model=slot.get("model") or "mock-screenwriter",
                temperature=0,
                json_mode=True,
            )
            return result.text[:180]
        if category == "image":
            from app.adapters.image.mock import MockImage

            image = MockImage().generate(
                prompt="测试连接",
                negative_prompt="",
                width=64,
                height=64,
                seed=1,
                reference_images=[],
                model="mock-image",
            )
            return f"本地出图 {image.width}×{image.height}，{len(image.data)} 字节"
        return "本地模拟已接通，不会访问外网"
    if category == "llm" and provider == "openai_compatible":
        from app.adapters.llm.openai_compatible import OpenAICompatibleLLM

        adapter = OpenAICompatibleLLM()
        adapter.apply_route(slot)
        result = adapter.complete(
            messages=[{"role": "user", "content": "只回复一个字：好"}],
            model=slot.get("model") or None,
            temperature=0,
            json_mode=False,
        )
        return (result.text or "").strip()[:180] or "空回复"
    if not slot.get("api_key"):
        raise RuntimeError("未填写 API Key")
    base = slot.get("base_url") or _env_base(category, provider)
    if not base:
        raise RuntimeError("未填写 Base URL")
    url = base if base.endswith("/models") else base.rstrip("/") + "/models"
    if provider == "openai_compatible":
        url = normalize_openai_base(base) + "/models"
    headers = {"Authorization": f"Bearer {slot['api_key']}"}
    if provider == "elevenlabs":
        headers = {"xi-api-key": slot["api_key"]}
        url = (base or "https://api.elevenlabs.io").rstrip("/") + "/v1/models"
    response = httpx.get(url, headers=headers, timeout=15)
    snippet = response.text[:180].replace("\n", " ")
    if response.status_code == 401:
        raise RuntimeError(f"401 密钥被拒绝 {snippet}")
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {snippet}")
    return f"HTTP {response.status_code} {snippet}"
