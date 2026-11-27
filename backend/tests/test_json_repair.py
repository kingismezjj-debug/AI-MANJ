import json
from types import SimpleNamespace

from app.pipeline.schema import complete_json, parse_screenplay


def _play() -> dict:
    return {
        "title": "杨飞",
        "logline": "谢谢你，苏桐。",
        "characters": [{"name": "苏桐", "appearance": "短发"}],
        "locations": [{"name": "教室", "description": "白天"}],
        "episodes": [
            {
                "number": 1,
                "title": "见面",
                "hook": "谢谢你",
                "cliffhanger": "她回头",
                "synopsis": "苏桐",
                "scenes": [
                    {
                        "number": 1,
                        "location": "教室",
                        "shots": [
                            {
                                "description": "苏桐点头",
                                "dialogue": "谢谢你，苏桐。",
                                "speaker": "杨飞",
                                "visual_prompt": "教室",
                                "characters": ["苏桐"],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_unwraps_response_format_string():
    wrapped = json.dumps({"type": "json_object", "content": json.dumps(_play(), ensure_ascii=False)}, ensure_ascii=False)
    play = parse_screenplay(wrapped)
    assert play.characters[0].name == "苏桐"
    assert play.episodes[0].scenes[0].shots[0].dialogue == "谢谢你，苏桐。"


def test_unwraps_response_format_dict():
    wrapped = json.dumps({"type": "json_object", "screenplay": _play()}, ensure_ascii=False)
    play = parse_screenplay(wrapped)
    assert play.title == "杨飞"
    assert play.episodes[0].scenes[0].shots[0].speaker == "杨飞"


def test_repair_retries_then_accepts(caplog):
    good = json.dumps(_play(), ensure_ascii=False)
    bad = json.dumps({"type": "json_object", "content": "谢谢你，苏桐。"}, ensure_ascii=False)

    class Router:
        def __init__(self):
            self.calls = 0

        def llm(self, messages, **kwargs):
            self.calls += 1
            text = bad if self.calls == 1 else good
            return SimpleNamespace(text=text)

    router = Router()
    play = complete_json(router, [{"role": "user", "content": "写剧本 json"}], validate=lambda payload: parse_screenplay(json.dumps(payload)), what="剧本")
    assert router.calls == 2
    assert play.episodes[0].scenes[0].shots[0].dialogue == "谢谢你，苏桐。"
    assert "raw=" in caplog.text


def test_gives_up_in_chinese_after_two_repairs():
    class Router:
        def llm(self, messages, **kwargs):
            return SimpleNamespace(text=json.dumps({"type": "json_object", "content": "谢谢你，苏桐。"}))

    try:
        complete_json(Router(), [{"role": "user", "content": "json"}], validate=lambda payload: parse_screenplay(json.dumps(payload)), what="剧本")
        raise AssertionError("should fail")
    except ValueError as exc:
        assert "请点重试" in str(exc)
        assert "剧本" in str(exc)
