import io
import json
import zipfile

from app.adapters.pricing import forecast_novel
from app.adapters.registry import preset_config
from app.billing import hold_for_run, settle_project, top_up
from app.db.models import Job, ModelCall, Project, Shot, User
from app.export.pack import build_zip
from app.pipeline.artifacts import save_bytes
from app.pipeline.controls import approve_script
from app.pipeline.executor import execute_job
from app.pipeline.graph import kick
from app.pipeline.names import CHARACTER_LOCK, LIPSYNC, SCRIPT_LOCK, SHOT_QC, STORYBOARD, VIDEO, VOICE
from app.pipeline.regen import regenerate_shot_stage
from app.pipeline.visual import compose_video_prompt, derive_anchors
from app.storage import get_store
from tests.test_dag import _job, _mark, seed_project


def test_script_checkpoint_blocks_downstream(db):
    project = seed_project(db, approval=False)
    settings = dict(project.settings)
    settings["require_script_approval"] = True
    settings["script_approved"] = False
    project.settings = settings
    db.commit()
    kick(project.id)
    db.expire_all()
    lock = _job(db, project.id, SCRIPT_LOCK)
    assert lock.status == "queued"
    execute_job(lock.id)
    db.expire_all()
    project = db.get(Project, project.id)
    assert project.status == "awaiting_script"
    assert _job(db, project.id, STORYBOARD).status == "blocked"
    approve_script(db, project.id)
    db.expire_all()
    execute_job(_job(db, project.id, SCRIPT_LOCK).id)
    db.expire_all()
    assert db.get(Project, project.id).status == "running"
    assert _job(db, project.id, "character_bible").status == "queued"


def test_manual_regen_touches_only_one_shot(db):
    project = seed_project(db, shots=2)
    for job in db.query(Job).filter(Job.project_id == project.id).all():
        if job.stage not in {"assemble_episode", "assemble_final", "qc_final"}:
            _mark(db, job)
    shots = db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.number).all()
    regenerate_shot_stage(db, shots[0], "video", prompt="改成雨里回头")
    db.commit()
    kick(project.id)
    db.expire_all()
    first = db.query(Shot).filter(Shot.id == shots[0].id).one()
    assert "雨里回头" in first.visual_prompt
    assert "口型" in first.video_prompt
    new_video = db.query(Job).filter(Job.idempotency_key == f"video:{first.id}:r1").one()
    assert new_video.status == "queued"
    second_videos = db.query(Job).filter(Job.entity_id == shots[1].id, Job.stage == VIDEO).all()
    assert len(second_videos) == 1
    assert second_videos[0].status == "succeeded"
    assemble = _job(db, project.id, "assemble_episode")
    new_qc = db.query(Job).filter(Job.idempotency_key == f"shot_qc:{first.id}:r1").one()
    assert new_qc.id in assemble.depends_on
    assert assemble.status == "blocked"


def test_forecast_breaks_down_by_stage():
    quote = forecast_novel(preset_config("cn_stack"), {"max_episodes": 1, "max_shots_per_episode": 4}, 400)
    assert quote["total_usd"] > 0
    assert {line["stage"] for line in quote["lines"]} >= {"编剧", "定妆与分镜", "视频", "配音", "口型"}
    mock = forecast_novel(preset_config("mock"), {}, 400)
    assert mock["total_usd"] == 0
    assert mock["reference_usd"] > 0


def test_credit_hold_then_settle(db):
    user = User(name="小记", role="user", api_key="poor-key", credit_balance=0.01)
    project = Project(title="账单", provider_config=preset_config("cn_stack"), settings={}, user_id=None)
    db.add(user)
    db.add(project)
    db.commit()
    project.user_id = user.id
    quote = forecast_novel(project.provider_config, {}, 2000)
    try:
        hold_for_run(db, user, project, quote["total_usd"])
        raise AssertionError("should refuse")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 402
    db.rollback()
    user = db.query(User).filter(User.api_key == "poor-key").one()
    project = db.query(Project).filter(Project.title == "账单").one()
    project.user_id = user.id
    db.commit()
    top_up(db, user, 50, "测试充值")
    user = db.get(User, user.id)
    held = hold_for_run(db, user, project, 2)
    db.commit()
    assert held == round(2 * 1.2, 6)
    db.add(
        ModelCall(
            project_id=project.id,
            category="video",
            provider="seedance",
            model="seedance",
            video_seconds=4,
            cost_usd=0.2,
            status="succeeded",
        )
    )
    db.commit()
    result = settle_project(db, project)
    db.commit()
    user = db.get(User, user.id)
    assert result["charge"] == round(0.2 * 1.2, 6)
    assert abs(user.credit_balance - (50.01 - result["charge"])) < 1e-6


def test_jianying_and_editorial_zip(db):
    project = seed_project(db, shots=1)
    shot = db.query(Shot).filter(Shot.project_id == project.id).one()
    shot.dialogue = "三年了"
    shot.speaker = "林夏"
    db.commit()
    store = get_store()
    save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="lipsync",
        data=b"fake-mp4",
        content_type="video/mp4",
        job_id=None,
        meta={"duration_sec": 2.5},
    )
    save_bytes(
        db,
        store,
        project_id=project.id,
        entity_type="shot",
        entity_id=shot.id,
        kind="voice",
        data=b"RIFFvoice",
        content_type="audio/wav",
        job_id=None,
        meta={"duration_sec": 2.5},
    )
    db.commit()
    package = build_zip(db, project, store, "jianying")
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        names = set(archive.namelist())
        assert "draft_content.json" in names
        assert "draft_info.json" in names
        assert "timeline.fcpxml" in names
        assert "timeline.edl" in names
        assert "subtitles.srt" in names
        draft = json.loads(archive.read("draft_content.json"))
        assert draft["canvas_config"]["ratio"] == "9:16"
        assert draft["platform"]["app_source"] == "lv"
        assert draft["tracks"][0]["type"] == "video"
        assert draft["tracks"][0]["segments"][0]["target_timerange"]["duration"] == 2_500_000
        assert "三年了" in archive.read("subtitles.srt").decode("utf-8")
        assert "asset-clip" in archive.read("timeline.fcpxml").decode("utf-8") or "asset-clip" in archive.read("timeline.fcpxml").decode("utf-8")
        assert b"fake-mp4" in archive.read([name for name in names if name.endswith(".mp4")][0])


def test_anchor_prompt_names_face_hair_outfit_and_timing():
    class Character:
        name = "林夏"
        appearance = "黑长直，米色风衣"
        anchors = derive_anchors(appearance)

    class ShotLike:
        description = "在雨里回头"
        dialogue = "三年了"
        speaker = "林夏"
        camera = "近景"
        duration_sec = 2.5
        character_names = ["林夏"]

    prompt, timing = compose_video_prompt(ShotLike(), [Character()])
    assert "发型=黑长直" in prompt
    assert "服装=米色风衣" in prompt
    assert "色板=" in prompt
    assert "口型" in prompt
    assert timing["start_sec"] == 0.2
    assert timing["end_sec"] > timing["start_sec"]
    assert {CHARACTER_LOCK, LIPSYNC, SHOT_QC, VOICE}
