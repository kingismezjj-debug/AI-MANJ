def test_catalog_and_draft_flow(client):
    catalog = client.get("/api/providers").json()
    providers = {item["provider"] for item in catalog["providers"]}
    assert {"mock", "seedream", "seedance", "kling", "http_poll", "minimax", "elevenlabs", "openai_compatible"} <= providers
    created = client.post(
        "/api/projects",
        json={"title": "草稿", "preset": "mock", "require_bible_approval": True},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]
    assert created.json()["settings"]["require_bible_approval"] is True
    denied = client.post(f"/api/projects/{project_id}/run")
    assert denied.status_code == 400
    sample = client.post(f"/api/projects/{project_id}/novel/sample")
    assert sample.status_code == 200
    health = client.get("/api/health")
    assert health.json()["ok"] is True
