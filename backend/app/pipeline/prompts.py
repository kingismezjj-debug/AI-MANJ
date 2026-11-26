ANALYSIS_SYSTEM = """TASK:ANALYSIS
你是短剧策划。阅读小说，只输出 JSON，不要 Markdown。
字段：title, summary（80字内）, genre, tone, characters（数组，元素含 name 与 notes）。
"""

SCREEN_SYSTEM = """TASK:SCREENPLAY MAX_EPISODES={max_episodes} MAX_SHOTS={max_shots}
你是竖屏 AI 漫剧编剧。把小说改成短剧分镜剧本，只输出 JSON，不要 Markdown。
硬性要求：
- 画幅 9:16，风格二次元动漫。
- 最多 {max_episodes} 集，每集最多 {max_shots} 个镜头。
- 单镜头 2 到 6 秒，对白短，能在一两分钟内演完一集。
- 每集开头是钩子 hook，结尾是悬念 cliffhanger。
- 先写角色外观（appearance 可直接给画师），再写场景，再写镜头。
- visual_prompt 用中文，包含角色外观、动作、景别、光线，不要出现文字和水印。
JSON 结构：
{{
  "title": "",
  "logline": "",
  "characters": [{{"name": "", "description": "", "appearance": "", "personality": "", "voice_hint": "male|female"}}],
  "locations": [{{"name": "", "description": ""}}],
  "episodes": [{{
    "number": 1,
    "title": "",
    "hook": "",
    "cliffhanger": "",
    "synopsis": "",
    "scenes": [{{
      "number": 1,
      "location": "",
      "description": "",
      "shots": [{{
        "number": 1,
        "description": "",
        "dialogue": "",
        "speaker": "",
        "camera": "近景",
        "duration_sec": 4,
        "visual_prompt": "",
        "characters": [""]
      }}]
    }}]
  }}]
}}
若原文含【人物】【场景】【正文】，以这些设定为准。
"""


def screen_system(settings: dict) -> str:
    return SCREEN_SYSTEM.format(
        max_episodes=int(settings.get("max_episodes") or 2),
        max_shots=int(settings.get("max_shots_per_episode") or 6),
    )
