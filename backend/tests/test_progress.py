from datetime import datetime, timedelta, timezone

from app.pipeline.progress import eta_seconds, overall_percent, progress_view


def test_map_progress_moves_before_the_job_finishes():
    early = overall_percent(
        status="running",
        stored={"stage": "map", "done": 0, "total": 120},
        ingest_done=False,
        director_done=False,
        script_done=False,
        cast_done=0,
        cast_total=0,
        storyboard_done=0,
        storyboard_total=0,
        video_done=0,
        video_total=0,
        finish_done=False,
    )
    later = overall_percent(
        status="running",
        stored={"stage": "map", "done": 37, "total": 120},
        ingest_done=False,
        director_done=False,
        script_done=False,
        cast_done=0,
        cast_total=0,
        storyboard_done=0,
        storyboard_total=0,
        video_done=0,
        video_total=0,
        finish_done=False,
    )
    assert early == 0
    assert later > early
    assert later == round(40 * 37 / 120, 1)


def test_storyboard_weight_is_added_after_the_bible():
    percent = overall_percent(
        status="running",
        stored={"stage": "reduce", "done": 1, "total": 1},
        ingest_done=True,
        director_done=True,
        script_done=True,
        cast_done=2,
        cast_total=2,
        storyboard_done=12,
        storyboard_total=36,
        video_done=0,
        video_total=36,
        finish_done=False,
    )
    assert percent > 40
    assert percent < 100


def test_old_project_without_a_counter_is_indeterminate():
    assert (
        overall_percent(
            status="running",
            stored=None,
            ingest_done=False,
            director_done=False,
            script_done=False,
            cast_done=0,
            cast_total=0,
            storyboard_done=0,
            storyboard_total=0,
            video_done=0,
            video_total=0,
            finish_done=False,
        )
        is None
    )


def test_stall_and_failure_and_eta():
    now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    stalled = progress_view(
        status="running",
        error=None,
        stored={"updated_at": (now - timedelta(seconds=200)).isoformat(), "label": "分析章节 3/10"},
        now=now,
        last_call={"provider": "openai_compatible", "model": "deepseek-chat", "status": "succeeded", "at": (now - timedelta(seconds=200)).isoformat()},
        stall_seconds=180,
        jobs_exist=True,
        label="分析章节 3/10",
        percent=12.0,
        eta=90,
    )
    assert stalled["stalled"] is True
    assert stalled["label"].startswith("疑似卡住")
    assert stalled["seconds_ago"] == 200
    assert stalled["eta_sec"] == 90
    assert stalled["last_call"]["model"] == "deepseek-chat"
    failed = progress_view(
        status="failed",
        error="ingest: boom",
        stored=None,
        now=now,
        last_call=None,
        stall_seconds=180,
        jobs_exist=True,
        label="制作中",
        percent=4.0,
        eta=10,
    )
    assert failed["state"] == "failed"
    assert failed["label"] == "出错"
    assert failed["error"] == "ingest: boom"
    assert failed["eta_sec"] is None
    assert eta_seconds(37, 120, [2000, 3000], concurrency=6) == max(1, int(83 * 2.5 / 6))
    assert eta_seconds(0, 120, [2000], 6) is None
