import json

"""Character anchors and the structured video prompt.

Every image and video call repeats the locked face, hair, outfit, and palette,
and the video prompt states when the mouth is allowed to move. That timing is
the same window the voice track uses.
"""

from app.db.models import Character, Shot


def as_dict(value) -> dict:
    """JSON columns added to an existing database can come back as text."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def derive_anchors(appearance: str) -> dict:
    text = appearance or ""
    if "黑长直" in text:
        hair = "黑长直"
    elif "短" in text:
        hair = "短碎发"
    else:
        hair = "与定妆一致的发型"
    if "风衣" in text:
        outfit = "米色风衣"
    elif "大衣" in text:
        outfit = "深灰大衣"
    else:
        outfit = "与定妆一致的服装"
    if "米色" in text:
        palette = "米色、暖黄、雨夜蓝灰"
    elif "深灰" in text or "墨" in text:
        palette = "深灰、墨色、暖黄"
    else:
        palette = "角色专属色板，全片不变"
    return {"face": text or "与定妆一致的五官", "hair": hair, "outfit": outfit, "palette": palette}


def normalize_anchors(appearance: str, raw: dict | None) -> dict:
    derived = derive_anchors(appearance)
    raw = as_dict(raw)
    return {key: (raw.get(key) or derived[key]) for key in ("face", "hair", "outfit", "palette")}


def dialogue_window(duration_sec: float, text: str) -> dict:
    duration = max(0.8, float(duration_sec or 2.5))
    spoken = (text or "").strip()
    if not spoken:
        return {"start_sec": 0.0, "end_sec": 0.0}
    speech = min(duration - 0.25, max(0.6, 0.18 * len(spoken)))
    start = 0.2
    end = min(duration, round(start + speech, 2))
    return {"start_sec": start, "end_sec": round(end, 2)}


def anchor_line(character: Character) -> str:
    anchors = as_dict(character.anchors) or derive_anchors(character.appearance or character.name)
    return (
        f"{character.name}：脸={anchors.get('face', '')}；"
        f"发型={anchors.get('hair', '')}；"
        f"服装={anchors.get('outfit', '')}；"
        f"色板={anchors.get('palette', '')}"
    )


def compose_video_prompt(shot: Shot, characters: list[Character]) -> tuple[str, dict]:
    timing = dialogue_window(shot.duration_sec, shot.dialogue)
    lines = [anchor_line(item) for item in characters]
    anchor_block = "\n".join(lines) if lines else "无具名角色，保持上一镜的造型。"
    speaker = shot.speaker or "旁白"
    spoken = (shot.dialogue or "").strip() or "（无对白）"
    prompt = (
        "[风格] 二次元动漫，竖屏9:16，角色外形必须与定妆锚点一致。\n"
        f"[锚点]\n{anchor_block}\n"
        f"[动作] {shot.description}\n"
        f"[镜头] {shot.camera}\n"
        f"[对白] {speaker}：{spoken}\n"
        f"[口型] 只在 {timing['start_sec']}s 到 {timing['end_sec']}s 张嘴，其余时间闭嘴。\n"
        "[负面] 文字，水印，多余肢体，换脸，换装，改色"
    )
    return prompt, timing


def apply_shot_prompt(db, shot: Shot) -> str:
    names = list(shot.character_names or [])
    if shot.speaker and shot.speaker not in names:
        names.append(shot.speaker)
    characters = []
    for name in names:
        character = (
            db.query(Character).filter(Character.project_id == shot.project_id, Character.name == name).one_or_none()
        )
        if character is not None:
            characters.append(character)
    prompt, timing = compose_video_prompt(shot, characters)
    shot.video_prompt = prompt
    shot.dialogue_timing = timing
    return prompt
