from app.adapters.capabilities import clamp_video_request
from app.adapters.keys import call_with_keys, mark_cooldown, split_keys
from app.adapters.pricing import forecast_novel
from app.db.models import Asset, Episode, Job, Novel, Project, Shot, User
from app.novel.chapters import llm_parts, split_chapters
from app.pipeline.assets import edit_asset, ensure_asset, rollback_asset
from app.pipeline.edits import apply_hunks, hunks
from app.pipeline.executor import execute_job
from app.pipeline.graph import ensure_director_job, ensure_ingest_job
from app.pipeline.longform import merged_groups, timestamped_prompt
from app.pipeline.stages import _novel_for_writer
from app.pipeline.stuck import note_pending, reap
from app.settings_store import choose_models


def test_long_text_is_not_cut_at_12k(db):
    ending = "林夏：结尾必须留下。"
    text = "甲" * 13000 + "\n" + ending + "\n"
    novel = Novel(project_id="p", filename="a.txt", text=text, char_count=len(text))
    assert ending in _novel_for_writer(novel, db)
    parts = llm_parts(text, size=6000)
    assert len(parts) > 1
    assert any(ending in part for part in parts)
    assert sum(len(part) for part in parts) >= len(text)


def test_chapters_stream_the_next_episode(db):
    book = "\n".join(
        [
            "第1章 雨\n林夏：第一集见。",
            "第2章 信\n陈默：第二集见。",
            "第3章 门\n林夏：第三集见。",
        ]
    )
    project = Project(
        title="连载",
        status="running",
        provider_config={},
        settings={"use_global_models": True, "episode_count": 60, "produce_first": 1, "shots_per_episode": 2, "fallback_to_mock": True},
    )
    db.add(project)
    db.flush()
    db.add(Novel(project_id=project.id, filename="a.txt", text=book, char_count=len(book)))
    ingest = ensure_ingest_job(db, project.id)
    ingest.status = "queued"
    db.commit()
    execute_job(ingest.id)
    db.expire_all()
    director = db.query(Job).filter(Job.project_id == project.id, Job.stage == "director").one()
    assert director.status == "queued"
    execute_job(director.id)
    db.expire_all()
    screen = db.query(Job).filter(Job.project_id == project.id, Job.stage == "screenwrite").one()
    execute_job(screen.id)
    db.expire_all()
    episodes = db.query(Episode).filter(Episode.project_id == project.id).order_by(Episode.number).all()
    assert len(episodes) == 3
    assert episodes[0].status != "outlined"
    assert episodes[1].status == "outlined"
    script = db.query(Job).filter(Job.project_id == project.id, Job.stage == "episode_script").all()
    assert script
    assert all("video" not in (job.stage) for job in script)
    assert all(screen.id in (job.depends_on or []) for job in script)
    video_ids = {job.id for job in db.query(Job).filter(Job.project_id == project.id, Job.stage == "video").all()}
    assert video_ids.isdisjoint(set(script[0].depends_on or []))


def test_relay_fills_three_slots_and_masks(client):
    saved = client.post(
        "/api/settings/relay",
        json={"base_url": "https://relay.example/v1", "api_key": "sk-relay-aaaa\nsk-relay-bbbb", "fetch_models": False},
    )
    assert saved.status_code == 200, saved.text
    body = saved.text
    assert "sk-relay-aaaa" not in body
    slots = {slot["category"]: slot for slot in saved.json()["slots"]}
    assert slots["llm"]["provider"] == "openai_compatible"
    assert slots["image"]["provider"] == "openai_compatible"
    assert slots["video"]["provider"] == "openai_compatible"
    assert slots["llm"]["model"] == "deepseek-chat"
    assert slots["llm"]["api_key_count"] == 2
    assert choose_models(["foo-chat", "bar-image", "baz-video"])["image"] == "bar-image"


def test_keys_rotate_and_cool_down():
    keys = split_keys("sk-one-1111\nsk-two-2222")
    seen = []

    def attempt(key):
        seen.append(key)
        if key.endswith("1111"):
            raise RuntimeError("429 too many")
        return "ok"

    assert call_with_keys("slot", keys, attempt) == "ok"
    assert seen[0].endswith("1111")
    mark_cooldown(seen[0], 60)
    seen.clear()

    def again(key):
        seen.append(key)
        return key

    chosen = call_with_keys("slot-b", keys, again)
    assert chosen.endswith("2222")
    assert all(not item.endswith("1111") for item in seen)


def test_asset_edit_stales_shot_and_rolls_back(db):
    project = Project(title="资产", settings={})
    db.add(project)
    db.flush()
    episode = Episode(project_id=project.id, number=1, title="一", status="ready")
    db.add(episode)
    db.flush()
    shot = Shot(project_id=project.id, episode_id=episode.id, scene_id="s", number=1, description="旧", visual_prompt="旧")
    db.add(shot)
    db.flush()
    asset = ensure_asset(db, project.id, "character", "林夏", "黑长直", "黑长直")
    from app.pipeline.assets import bind_shot

    bind_shot(db, shot, asset)
    db.commit()
    stale = edit_asset(db, asset, prompt="短发")
    assert shot.id in stale
    assert shot.stale is True
    rollback_asset(db, asset, 1)
    assert asset.prompt == "黑长直"
    library = Asset(project_id="", kind="character", name="可复用", description="卡", prompt="卡", version=1, meta={})
    db.add(library)
    db.commit()
    from app.pipeline.assets import import_card

    copied = import_card(db, project.id, library)
    assert copied.project_id == project.id
    assert copied.source_id == library.id


def test_rewrite_hunks_and_accept(client, db):
    created = client.post("/api/projects", json={"title": "改词", "preset": "mock"}).json()
    project = db.get(Project, created["id"])
    episode = Episode(project_id=project.id, number=1, title="一", status="scripted")
    db.add(episode)
    db.flush()
    shot = Shot(
        project_id=project.id,
        episode_id=episode.id,
        scene_id="s",
        number=1,
        description="他站在雨里",
        dialogue="原来的话",
        visual_prompt="雨",
    )
    db.add(shot)
    db.commit()
    response = client.post(
        f"/api/projects/{project.id}/rewrite",
        json={"shot_id": shot.id, "field": "description", "instruction": "更短", "text": "他站在雨里"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["hunks"]
    assert payload["affected"]["shot_ids"] == [shot.id]
    accepted = client.post(
        f"/api/projects/{project.id}/rewrite/accept",
        json={
            "shot_id": shot.id,
            "field": "description",
            "before": payload["before"],
            "proposed": payload["proposed"],
            "hunks": list(range(len(payload["hunks"]))),
        },
    )
    assert accepted.status_code == 200, accepted.text
    db.expire_all()
    fresh = db.get(Shot, shot.id)
    assert fresh.description.startswith("改：")
    assert fresh.stale is True
    diff = hunks("他站在雨里", "他离开了")
    assert apply_hunks("他站在雨里", "他离开了", []) == "他站在雨里"
    assert apply_hunks("他站在雨里", "他离开了", list(range(len(diff)))) == "他离开了"


def test_reference_clamp_and_merge_prompt():
    clamped = clamp_video_request("kling", references=[b"a", b"b", b"c"], last_image=b"z", duration_sec=40, merged=True)
    assert len(clamped["references"]) == 1
    assert clamped["last_image"] is None
    assert "不支持首尾帧" in clamped["notice"]
    seedance = clamp_video_request("seedance", references=[b"a"] * 12, last_image=b"z", duration_sec=28, merged=True)
    assert len(seedance["references"]) == 9
    assert seedance["last_image"] == b"z"
    assert seedance["duration_sec"] == 28
    shots = []
    for number in range(1, 7):
        shots.append(Shot(scene_id="same", number=number, duration_sec=3, description=f"镜头{number}", dialogue=""))
    groups = merged_groups(shots, floor_sec=15, ceiling_sec=30)
    assert groups[0][0].number == 1
    assert sum(item.duration_sec for item in groups[0]) >= 15
    prompt = timestamped_prompt(groups[0])
    assert prompt.startswith("[0.0-")
    quote = forecast_novel({"video": {"provider": "seedance"}}, {"max_episodes": 1, "max_shots_per_episode": 4}, 400)
    assert quote["final_usd"] == quote["total_usd"]
    assert quote["draft_usd"] < quote["final_usd"]


def test_stuck_job_refunds_once(db):
    user = User(name="观察", role="user", api_key="watch-key", credit_balance=1)
    project = Project(title="卡住", user_id=None, settings={})
    db.add(user)
    db.add(project)
    db.flush()
    project.user_id = user.id
    job = Job(project_id=project.id, stage="video", status="queued", entity_type="shot", entity_id="s", depends_on=[], idempotency_key="stuck-1")
    db.add(job)
    db.flush()
    note_pending(job, "task-9", held_usd=0.2)
    db.commit()
    assert job.status == "observing"
    for _ in range(2):
        reap(db, project.id)
    db.refresh(job)
    assert job.status == "observing"
    reap(db, project.id)
    db.refresh(job)
    assert job.status == "verifying"
    reap(db, project.id)
    reap(db, project.id)
    db.refresh(job)
    assert job.status == "failed"
    db.refresh(user)
    assert abs(user.credit_balance - 1.2) < 1e-6
    again = reap(db, project.id)
    assert again == []
    db.refresh(user)
    assert abs(user.credit_balance - 1.2) < 1e-6
