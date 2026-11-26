from app.db.models import AgentEvent, Novel, Project
from app.pipeline.executor import execute_job
from app.pipeline.graph import ensure_director_job, ensure_ingest_job
from app.settings_store import ensure_defaults, list_slots, mask_key, save_slots


def test_slot_save_persists_and_masks_key(db, client):
    saved = client.put(
        "/api/settings/slots",
        json={
            "slots": [
                {
                    "category": "llm",
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-reasoner",
                    "api_key": "sk-live-secret-key-9999",
                    "params": {},
                    "enabled": True,
                }
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    llm = next(slot for slot in body["slots"] if slot["category"] == "llm")
    assert llm["model"] == "deepseek-reasoner"
    assert llm["base_url"] == "https://api.deepseek.com/v1"
    assert llm["api_key_set"] is True
    assert "sk-live-secret-key-9999" not in saved.text
    assert llm["api_key_mask"] == mask_key("sk-live-secret-key-9999")
    again = client.put(
        "/api/settings/slots",
        json={
            "slots": [
                {
                    "category": "llm",
                    "provider": "openai_compatible",
                    "base_url": "https://relay.example/v1",
                    "model": "deepseek-chat",
                    "api_key": "",
                    "params": {},
                }
            ]
        },
    )
    llm = next(slot for slot in again.json()["slots"] if slot["category"] == "llm")
    assert llm["model"] == "deepseek-chat"
    assert llm["base_url"] == "https://relay.example/v1"
    assert llm["api_key_mask"] == mask_key("sk-live-secret-key-9999")
    listed = list_slots(db)
    assert "sk-live-secret-key-9999" not in str(listed)


def test_connection_mock_returns_latency(client):
    response = client.post(
        "/api/settings/slots/test",
        json={"category": "llm", "provider": "mock", "model": "mock-screenwriter", "base_url": "", "api_key": ""},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["latency_ms"] >= 0
    assert body["provider"] == "mock"
    assert "dispatch" in body["detail"] or "编剧" in body["detail"]


def test_director_dispatches_named_agents(db):
    ensure_defaults(db)
    project = Project(title="总控", status="running", provider_config={}, settings={"use_global_models": True})
    db.add(project)
    db.flush()
    db.add(Novel(project_id=project.id, filename="a.txt", text="雨下了一夜。林夏：你来了。", char_count=12))
    ingest = ensure_ingest_job(db, project.id)
    ingest.status = "succeeded"
    director = ensure_director_job(db, project.id, ingest.id)
    director.status = "queued"
    db.commit()
    execute_job(director.id)
    db.expire_all()
    messages = db.query(AgentEvent).filter(AgentEvent.project_id == project.id).all()
    text = "\n".join(item.content for item in messages)
    assert "派给编剧" in text
    assert "派给质检" in text
    from app.db.models import Job

    screen = db.query(Job).filter(Job.project_id == project.id, Job.stage == "screenwrite").one()
    assert director.id in screen.depends_on


def test_project_override_beats_global(db):
    ensure_defaults(db)
    project = Project(title="覆盖", settings={"use_global_models": True}, provider_config={})
    db.add(project)
    db.commit()
    save_slots(
        db,
        [{"category": "video", "provider": "kling", "model": "kling-v2-6", "base_url": "https://api-singapore.klingai.com", "api_key": "kling-secret-1234", "enabled": True, "params": {}}],
        project_id=project.id,
    )
    from app.settings_store import effective_slot

    slot = effective_slot(db, project, "video")
    assert slot["provider"] == "kling"
    assert slot["api_key"] == "kling-secret-1234"
    image = effective_slot(db, project, "image")
    assert image["provider"] == "mock"
