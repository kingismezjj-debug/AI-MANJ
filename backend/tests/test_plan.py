from app.db.models import ChapterCache, Episode, Job, Novel, Project, Shot
from app.novel.chapters import group_chapters, llm_segments, resolved_plan, split_chapters, suggest_episode_count
from app.pipeline.plan import apply_plan, cache_count


def test_short_headings_do_not_explode_and_merge_to_window():
    lines = []
    for index in range(1, 101):
        lines.append(f"第{index}章")
        lines.append("甲" * 500)
    text = "\n".join(lines)
    # A long line that merely starts like a heading stays one paragraph.
    text += "\n第1章 " + ("乙" * 80)
    chapters = split_chapters(text)
    assert len(chapters) == 100
    segments = llm_segments(text, size=24000, chapters=chapters)
    assert len(segments) < 20
    assert all(len(part) <= 24000 for _index, part in segments)
    assert suggest_episode_count(2614) == 80
    assert suggest_episode_count(30) == 30
    plan = resolved_plan(2614, {"episode_mode": "fixed", "episode_count": 20, "produce_first": 2})
    assert plan["episodes"] == 20
    assert plan["trial"] == 2
    groups = group_chapters(200, 20)
    assert len(groups) == 20
    assert groups[0] == (0, 10)


def test_apply_plan_keeps_cache_and_written_script(db):
    project = Project(title="杨飞", status="awaiting_script", settings={"episode_count": 2000, "produce_first": 1, "full_read": True}, provider_config={})
    db.add(project)
    db.flush()
    novel = Novel(project_id=project.id, filename="a.txt", text="第1章\n甲\n第2章\n乙\n第3章\n丙\n", char_count=12)
    db.add(novel)
    db.flush()
    episode = Episode(project_id=project.id, number=1, title="已写", status="scripted")
    db.add(episode)
    db.flush()
    db.add(Shot(project_id=project.id, episode_id=episode.id, scene_id="s", number=1, description="已有剧本", dialogue="谢谢你，苏桐。"))
    db.add(ChapterCache(project_id=project.id, chunk_hash="abc", prompt_version="map-v3", payload={"notes": "保留"}))
    db.add(Job(project_id=project.id, stage="bible_refine", status="running", entity_type="project", entity_id=project.id, depends_on=[], idempotency_key=f"bible_refine:{project.id}"))
    db.commit()
    before = cache_count(db, project.id)
    info = apply_plan(db, project, novel, {"episode_mode": "fixed", "episode_count": 2, "produce_first": 1, "full_read": False})
    db.expire_all()
    assert cache_count(db, project.id) == before
    assert db.query(Shot).filter(Shot.project_id == project.id).count() == 1
    refine = db.query(Job).filter(Job.project_id == project.id, Job.stage == "bible_refine").one()
    assert refine.status == "interrupted"
    assert info["episodes"] == 2
    assert info["full_read"] is False
    numbers = [row.number for row in db.query(Episode).filter(Episode.project_id == project.id).all()]
    assert 1 in numbers
    assert len(numbers) == 2
