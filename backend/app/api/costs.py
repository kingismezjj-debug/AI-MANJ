from app.adapters.pricing import REFERENCE_STACK, estimate_lines, estimate_units, estimate_usd, providers_of
from app.db.models import Character, Location, ModelCall, Shot


def cost_report(db, project) -> dict:
    from sqlalchemy import func

    grouped = (
        db.query(ModelCall.category, func.count(ModelCall.id), func.coalesce(func.sum(ModelCall.cost_usd), 0))
        .filter(ModelCall.project_id == project.id, ModelCall.status == "succeeded")
        .group_by(ModelCall.category)
        .all()
    )
    actual = round(sum(float(usd or 0) for _category, _count, usd in grouped), 6)
    by_category = {
        category: {"usd": round(float(usd or 0), 6), "calls": int(count)} for category, count, usd in grouped
    }
    shot_count, dialogue_chars, video_seconds = db.query(
        func.count(Shot.id),
        func.coalesce(func.sum(func.length(Shot.dialogue)), 0),
        func.coalesce(func.sum(Shot.duration_sec), 0),
    ).filter(Shot.project_id == project.id).one()
    characters = db.query(Character).filter(Character.project_id == project.id).count()
    locations = db.query(Location).filter(Location.project_id == project.id).count()
    calls = (
        db.query(ModelCall).filter(ModelCall.project_id == project.id).order_by(ModelCall.created_at.desc()).limit(40).all()
    )
    if shot_count:
        units = estimate_units(
            characters=characters,
            locations=locations,
            shots=int(shot_count),
            dialogue_chars=int(dialogue_chars or 0),
            video_seconds=float(video_seconds or 0),
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
