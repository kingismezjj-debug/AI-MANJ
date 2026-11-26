from app.adapters.pricing import REFERENCE_STACK, estimate_lines, estimate_units, estimate_usd, providers_of
from app.db.models import Character, Location, ModelCall, Shot


def cost_report(db, project) -> dict:
    calls = (
        db.query(ModelCall).filter(ModelCall.project_id == project.id).order_by(ModelCall.created_at.desc()).all()
    )
    succeeded = [call for call in calls if call.status == "succeeded"]
    actual = round(sum(call.cost_usd or 0 for call in succeeded), 6)
    by_category: dict[str, dict] = {}
    for call in succeeded:
        bucket = by_category.setdefault(call.category, {"usd": 0.0, "calls": 0})
        bucket["usd"] = round(bucket["usd"] + (call.cost_usd or 0), 6)
        bucket["calls"] += 1
    shots = db.query(Shot).filter(Shot.project_id == project.id).all()
    characters = db.query(Character).filter(Character.project_id == project.id).count()
    locations = db.query(Location).filter(Location.project_id == project.id).count()
    if shots:
        units = estimate_units(
            characters=characters,
            locations=locations,
            shots=len(shots),
            dialogue_chars=sum(len(shot.dialogue or "") for shot in shots),
            video_seconds=sum(shot.duration_sec or 0 for shot in shots),
        )
        preliminary = False
    else:
        units = estimate_units(characters=3, locations=1, shots=6, dialogue_chars=80, video_seconds=18)
        preliminary = True
    selected = providers_of(project.provider_config)
    lines = estimate_lines(selected, units)
    quote = (project.settings or {}).get("cost_quote")
    return {
        "actual_usd": actual,
        "estimated_usd": estimate_usd(selected, units),
        "reference_usd": estimate_usd(REFERENCE_STACK, units),
        "lines": lines,
        "quote": quote,
        "preliminary": preliminary,
        "by_category": by_category,
        "selected_providers": selected,
        "calls": [
            {
                "id": call.id,
                "category": call.category,
                "provider": call.provider,
                "model": call.model,
                "cost_usd": call.cost_usd,
                "status": call.status,
                "latency_ms": call.latency_ms,
                "error": call.error,
            }
            for call in calls[:40]
        ],
        "note": "金额是按 adapters/pricing.py 的单价做的美元估算，不是供应商账单。商用参考按兼容 LLM + Seedream + Seedance + MiniMax + 可灵口型计算。",
    }
