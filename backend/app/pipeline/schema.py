import json
import re

from pydantic import BaseModel, Field, ValidationError


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


def extract_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


def parse_screenplay(text: str) -> Screenplay:
    try:
        return Screenplay.model_validate(extract_json(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid screenplay JSON: {exc}") from exc


def clamp_screenplay(screenplay: Screenplay, settings: dict) -> Screenplay:
    max_episodes = max(1, int(settings.get("max_episodes") or 2))
    max_shots = max(1, int(settings.get("max_shots_per_episode") or 6))
    episodes = []
    for episode in screenplay.episodes[:max_episodes]:
        flat: list[tuple[SceneSpec, ShotSpec]] = []
        for scene in episode.scenes:
            for shot in scene.shots:
                shot.duration_sec = max(2.0, min(8.0, float(shot.duration_sec or 2.5)))
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
