import json
import re

from app.adapters.types import LLMResult, Usage

DIALOGUE_RE = re.compile(r"^([\u4e00-\u9fffA-Za-z·]{1,8})[：:](.+)$")


def _novel_from_prompt(blob: str) -> str:
    match = re.search(r"<<<NOVEL\n(.*)\n>>>", blob, re.S)
    return match.group(1).strip() if match else blob


def _split_marked(text: str) -> tuple[str, dict[str, str], dict[str, str], list[str]]:
    title = ""
    characters: dict[str, str] = {}
    locations: dict[str, str] = {}
    body: list[str] = []
    mode = "preamble"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("【人物】"):
            mode = "characters"
            continue
        if line.startswith("【场景】"):
            mode = "locations"
            continue
        if line.startswith("【正文】"):
            mode = "body"
            continue
        if mode == "preamble" and not title and not line.startswith("【"):
            title = line[:40]
            continue
        if mode == "characters" and "：" in line:
            name, desc = line.split("：", 1)
            characters[name.strip()] = desc.strip()
            continue
        if mode == "locations" and "：" in line:
            name, desc = line.split("：", 1)
            locations[name.strip()] = desc.strip()
            continue
        body.append(line)
    return title or "未命名", characters, locations, body


def screenplay_from_novel(text: str, max_shots: int = 6, max_episodes: int = 1) -> dict:
    title, characters, locations, body = _split_marked(text)
    known = set(characters)
    shots: list[dict] = []
    pending: list[str] = []

    def is_dialogue(line: str):
        match = DIALOGUE_RE.match(line)
        if not match:
            return None
        name = match.group(1)
        if known and name not in known:
            return None
        return name, match.group(2).strip()

    for line in body:
        spoken = is_dialogue(line)
        if spoken:
            name, line_text = spoken
            description = " ".join(pending).strip() or f"{name}说话"
            pending = []
            appearance = characters.get(name, f"二次元角色{name}")
            shots.append(
                {
                    "description": description,
                    "dialogue": line_text,
                    "speaker": name,
                    "camera": "中近景" if len(shots) % 2 == 0 else "近景",
                    "duration_sec": 2.5,
                    "visual_prompt": f"{appearance}。{description}。竖屏9:16，二次元动漫，电影感光影，不要文字。",
                    "characters": [name],
                }
            )
        else:
            pending.append(line)
    if pending and len(shots) < max_shots:
        description = " ".join(pending).strip()
        shots.append(
            {
                "description": description,
                "dialogue": "",
                "speaker": "",
                "camera": "全景",
                "duration_sec": 2.5,
                "visual_prompt": f"{description}。竖屏9:16，二次元动漫，环境空镜，不要文字。",
                "characters": list(characters)[:2],
            }
        )
    if not shots:
        excerpt = text.strip().replace("\n", " ")[:80] or "空镜"
        shots.append(
            {
                "description": excerpt,
                "dialogue": "",
                "speaker": "",
                "camera": "中景",
                "duration_sec": 2.5,
                "visual_prompt": f"{excerpt}。竖屏9:16，二次元动漫，不要文字。",
                "characters": [],
            }
        )
    shots = shots[:max_shots]
    for index, shot in enumerate(shots, start=1):
        shot["number"] = index

    if not characters:
        names = []
        for shot in shots:
            if shot["speaker"] and shot["speaker"] not in names:
                names.append(shot["speaker"])
        if not names:
            names = ["主角"]
        for name in names:
            characters[name] = f"二次元短剧角色{name}，特征鲜明的发型与服装"
    if not locations:
        locations = {"旧楼楼道": "雨夜，暖黄壁灯，墙皮剥落"}

    cast = []
    for name, appearance in characters.items():
        cast.append(
            {
                "name": name,
                "description": appearance,
                "appearance": appearance,
                "personality": "克制",
                "voice_hint": "female" if "夏" in name or "女" in appearance else "male",
            }
        )
    location_specs = [{"name": name, "description": desc} for name, desc in locations.items()]
    first_location = location_specs[0]["name"]
    hook = shots[0]["dialogue"] or shots[0]["description"]
    cliff = shots[-1]["dialogue"] or shots[-1]["description"]
    episode = {
        "number": 1,
        "title": title,
        "hook": hook[:80],
        "cliffhanger": cliff[:80],
        "synopsis": (body[0] if body else title)[:120],
        "scenes": [
            {
                "number": 1,
                "location": first_location,
                "description": locations[first_location],
                "shots": shots,
            }
        ],
    }
    return {
        "title": title,
        "logline": hook[:80],
        "characters": cast,
        "locations": location_specs,
        "episodes": [episode][:max_episodes],
    }


def analysis_from_novel(text: str) -> dict:
    title, characters, _locations, body = _split_marked(text)
    summary_source = " ".join(body)[:180] or text[:180]
    return {
        "title": title,
        "summary": summary_source,
        "genre": "都市情感短剧",
        "tone": "雨夜、克制、悬念",
        "characters": [{"name": name, "notes": notes} for name, notes in characters.items()],
    }


class MockLLM:
    provider = "mock"

    def complete(self, *, messages, model, temperature, json_mode) -> LLMResult:
        blob = "\n".join(message.get("content", "") for message in messages)
        novel = _novel_from_prompt(blob)
        if "TASK:SCREENPLAY" in blob:
            max_shots = _int_flag(blob, "MAX_SHOTS", 6)
            max_episodes = _int_flag(blob, "MAX_EPISODES", 1)
            payload = screenplay_from_novel(novel, max_shots=max_shots, max_episodes=max_episodes)
        else:
            payload = analysis_from_novel(novel)
        text = json.dumps(payload, ensure_ascii=False)
        usage = Usage(input_tokens=max(1, len(blob) // 2), output_tokens=max(1, len(text) // 2))
        return LLMResult(text=text, usage=usage, provider=self.provider, model=model or "mock-screenwriter")


def _int_flag(blob: str, name: str, default: int) -> int:
    match = re.search(rf"{name}=(\d+)", blob)
    return int(match.group(1)) if match else default
