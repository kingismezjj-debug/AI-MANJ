import json
import logging
import re

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

WRAPPER_KEYS = ("content", "data", "result", "json", "screenplay", "output", "message", "text", "payload", "response")


class ShotSpec(BaseModel):
    number: int = 1
    description: str
    dialogue: str = ""
    speaker: str = ""
    camera: str = "中景"
    duration_sec: float = 2.5
    visual_prompt: str
    characters: list[str] = Field(default_factory=list)


class SceneSpec(BaseModel):
    number: int = 1
    location: str
    description: str = ""
    shots: list[ShotSpec]


class EpisodeSpec(BaseModel):
    number: int = 1
    title: str
    hook: str = ""
    cliffhanger: str = ""
    synopsis: str = ""
    scenes: list[SceneSpec]


class CharacterSpec(BaseModel):
    name: str
    description: str = ""
    appearance: str = ""
    personality: str = ""
    voice_hint: str = ""
    anchors: dict = Field(default_factory=dict)


class LocationSpec(BaseModel):
    name: str
    description: str = ""


class Screenplay(BaseModel):
    title: str
    logline: str = ""
    characters: list[CharacterSpec]
    locations: list[LocationSpec] = Field(default_factory=list)
    episodes: list[EpisodeSpec]


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_object(raw: str):
    text = _strip_fences(raw)
    if not text:
        return None
    try:
        if text[0] in "{[":
            parsed = json.loads(text)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return None
            parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        parsed = next((item for item in parsed if isinstance(item, dict)), None)
    return parsed if isinstance(parsed, dict) else None


def _has_body(value: dict) -> bool:
    return any(key in value for key in ("characters", "episodes", "notes", "bible", "summary", "title", "dispatch", "scenes", "text"))


def _unwrap(value: dict, depth: int = 0) -> dict:
    if depth > 5 or _has_body(value):
        return value
    if value.get("type") == "json_object":
        for key, inner in value.items():
            if key == "type":
                continue
            parsed = inner if isinstance(inner, dict) else _parse_object(inner) if isinstance(inner, str) else None
            if isinstance(parsed, dict):
                return _unwrap(parsed, depth + 1)
    for key in WRAPPER_KEYS:
        if key not in value:
            continue
        inner = value[key]
        parsed = inner if isinstance(inner, dict) else _parse_object(inner) if isinstance(inner, str) else None
        if isinstance(parsed, dict) and parsed is not value:
            return _unwrap(parsed, depth + 1)
    return value


def extract_json(text: str) -> dict:
    parsed = _parse_object(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False))
    if parsed is None:
        logger.warning("llm json parse failed raw=%s", str(text)[:800])
        raise ValueError("模型没有返回 JSON 对象")
    return _unwrap(parsed)


def _plain_error(exc: Exception) -> str:
    text = str(exc)
    bits = []
    if "characters" in text and "Field required" in text:
        bits.append("缺少角色 characters")
    if "episodes" in text and "Field required" in text:
        bits.append("缺少分集 episodes")
    if "title" in text and "Field required" in text:
        bits.append("缺少标题 title")
    return "；".join(bits) or text[:180]


def parse_screenplay(text: str) -> Screenplay:
    try:
        return Screenplay.model_validate(extract_json(text))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        logger.warning("screenplay json invalid: %s raw=%s", exc, str(text)[:800])
        raise ValueError(f"剧本 JSON 不合格：{_plain_error(exc)}") from exc


def complete_json(router, messages, *, validate=None, temperature: float = 0.4, model: str | None = None, max_tokens: int | None = None, what: str = "结果"):
    """Call the model, unwrap JSON, and repair up to twice. Each call is metered."""
    current = list(messages)
    last_text = ""
    last_error = ""
    for attempt in range(3):
        result = router.llm(
            current,
            json_mode=True,
            temperature=0 if attempt else temperature,
            model=model,
            max_tokens=max_tokens,
        )
        last_text = result.text or ""
        try:
            payload = extract_json(last_text)
            if validate is not None:
                return validate(payload)
            return payload
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = _plain_error(exc)
            logger.warning("llm json %s attempt %s failed: %s raw=%s", what, attempt + 1, last_error, last_text[:800])
            current = [
                {
                    "role": "system",
                    "content": "只输出一个 json 对象。不要 Markdown，不要 {\"type\":\"json_object\"} 这种包装。",
                },
                {
                    "role": "user",
                    "content": f"校验错误：\n{last_error}\n\n错误输出：\n{last_text[:6000]}\n\n请输出修正后的 json。",
                },
            ]
    logger.error("llm json %s gave up raw=%s", what, last_text[:800])
    raise ValueError(f"{what}没有通过校验：{last_error}。请点重试。")


def clamp_screenplay(screenplay: Screenplay, settings: dict) -> Screenplay:
    max_episodes = max(1, int(settings.get("max_episodes") or 2))
    max_shots = max(1, int(settings.get("max_shots_per_episode") or 6))
    episodes = []
    for episode in screenplay.episodes[:max_episodes]:
        flat: list[tuple[SceneSpec, ShotSpec]] = []
        for scene in episode.scenes:
            for shot in scene.shots:
                shot.duration_sec = max(2.0, min(10.0, float(shot.duration_sec or 2.5)))
                flat.append((scene, shot))
        flat = flat[:max_shots]
        scenes: list[SceneSpec] = []
        for scene, shot in flat:
            if not scenes or scenes[-1].number != scene.number:
                scenes.append(SceneSpec(number=scene.number, location=scene.location, description=scene.description, shots=[]))
            scenes[-1].shots.append(shot)
        number = 1
        for scene in scenes:
            for shot in scene.shots:
                shot.number = number
                number += 1
        episode.scenes = scenes
        episodes.append(episode)
    screenplay.episodes = episodes
    if not screenplay.characters:
        screenplay.characters = [CharacterSpec(name="主角", appearance="二次元短剧主角，特征鲜明")]
    if not screenplay.locations:
        screenplay.locations = [LocationSpec(name="未命名场景", description="竖屏动漫场景")]
    return screenplay
