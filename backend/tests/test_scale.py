"""A project the size of a runaway book must stay cheap to open, then collapse."""

import time

from sqlalchemy import event, insert

from app.api.board import build_board
from app.db.models import ChapterCache, Episode, Job, Project, Shot
from app.db.session import engine
from app.pipeline.executor import execute_job
from app.pipeline.plan import compact_overgrown
from app.util import new_id, utcnow


def test_large_project_board_is_bounded_and_compact_keeps_the_trial(db):
    now = utcnow()
    project = Project(
        title="杨飞",
        status="awaiting_script",
        settings={"episode_count": 80, "produce_first": 2, "full_read": False, "episode_mode": "auto"},
        provider_config={},
    )
    db.add(project)
    db.flush()
    episodes = []
    shots = []
    shot_of = {}
    for number in range(1, 2001):
        episode_id = new_id()
        shot_id = new_id()
        shot_of[number] = shot_id
        episodes.append(
            {
                "id": episode_id,
                "project_id": project.id,
                "number": number,
                "title": f"第{number}集",
                "hook": "",
                "cliffhanger": "",
                "synopsis": "",
                "status": "scripted" if number <= 2 else "outlined",
            }
        )
        shots.append(
            {
                "id": shot_id,
                "project_id": project.id,
                "episode_id": episode_id,
                "scene_id": "scene",
                "number": 1,
                "description": "谢谢你，苏桐。" if number <= 2 else "",
                "dialogue": "谢谢你，苏桐。" if number <= 2 else "",
                "speaker": "",
                "camera": "中景",
                "duration_sec": 3,
                "visual_prompt": "",
                "video_prompt": "",
                "dialogue_timing": {},
                "character_names": [],
                "revision": 0,
                "stage_status": {},
                "qc_attempts": 0,
                "seed": 1,
                "stale": False,
                "quality": "final",
            }
        )
    db.execute(insert(Episode), episodes)
    db.execute(insert(Shot), shots)
    jobs = [
        {
            "id": new_id(),
            "project_id": project.id,
            "stage": "bible_refine",
            "status": "running",
            "entity_type": "project",
            "entity_id": project.id,
            "depends_on": [],
            "idempotency_key": f"bible_refine:{project.id}",
            "attempt": 0,
            "max_attempts": 2,
            "error": None,
            "result": {},
            "enqueued": True,
            "created_at": now,
            "updated_at": now,
        }
    ]
    for index in range(20):
        jobs.append(
            {
                "id": new_id(),
                "project_id": project.id,
                "stage": "storyboard",
                "status": "succeeded",
                "entity_type": "shot",
                "entity_id": shot_of[1],
                "depends_on": [],
                "idempotency_key": f"keep-{index}",
                "attempt": 0,
                "max_attempts": 2,
                "error": None,
                "result": {},
                "enqueued": False,
                "created_at": now,
                "updated_at": now,
            }
        )
    for index in range(40000):
        jobs.append(
            {
                "id": new_id(),
                "project_id": project.id,
                "stage": "video",
                "status": "queued" if index % 2 == 0 else "blocked",
                "entity_type": "shot",
                "entity_id": shot_of[3 + (index % 1998)],
                "depends_on": [],
                "idempotency_key": f"runaway-{index}",
                "attempt": 0,
                "max_attempts": 2,
                "error": None,
                "result": {},
                "enqueued": True,
                "created_at": now,
                "updated_at": now,
            }
        )
    for start in range(0, len(jobs), 2000):
        db.execute(insert(Job), jobs[start : start + 2000])
    db.add(ChapterCache(project_id=project.id, chunk_hash="abc", prompt_version="map-v3", payload={"notes": "保留"}))
    db.commit()
    dropped = jobs[-1]["id"]

    statements: list[str] = []

    def _count(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    started = time.perf_counter()
    try:
        board = build_board(db, project, limit=20)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", _count)

    selects = [item for item in statements if item.lstrip().lower().startswith("select")]
    assert elapsed < 0.5, elapsed
    assert len(selects) < 40, len(selects)
    assert board["episode_total"] == 2000
    assert len(board["episodes"]) == 20
    assert board["episodes"][0]["shots"][0]["dialogue"] == "谢谢你，苏桐。"

    changed = compact_overgrown(db)
    assert changed == [project.id]
    db.expire_all()
    assert db.query(Episode).filter(Episode.project_id == project.id).count() == 80
    assert db.query(Shot).filter(Shot.project_id == project.id).count() == 2
    assert db.query(ChapterCache).filter(ChapterCache.project_id == project.id).count() == 1
    refine = db.query(Job).filter(Job.project_id == project.id, Job.stage == "bible_refine").one()
    assert refine.status == "interrupted"
    assert db.query(Job).filter(Job.project_id == project.id, Job.entity_id == shot_of[1]).count() == 20
    assert db.query(Job).filter(Job.project_id == project.id, Job.status.in_(["queued", "running", "blocked"])).count() == 0
    assert compact_overgrown(db) == []
    before = db.query(Job).count()
    assert execute_job(dropped) == "missing"
    assert db.query(Job).count() == before
