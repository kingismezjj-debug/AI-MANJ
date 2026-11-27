"""Visible multi-agent desk.

The director is the LLM slot. It plans the production and the stage handlers
record every dispatch and reply. The agent page reads this table plus model_calls.
"""

from app.db.models import AgentEvent, Job
from app.pipeline.events import publish

AGENT_FOR_STAGE = {
    "ingest": "director",
    "bible_refine": "director",
    "director": "director",
    "screenwrite": "screenwriter",
    "episode_script": "screenwriter",
    "script_lock": "director",
    "character_bible": "bible",
    "location_bible": "bible",
    "character_lock": "bible",
    "storyboard": "storyboard",
    "animatic": "storyboard",
    "video": "video",
    "tts_temp": "voice",
    "voice": "voice",
    "lipsync": "voice",
    "music": "voice",
    "shot_qc": "qc",
    "qc_final": "qc",
    "assemble_episode": "director",
    "assemble_final": "director",
}

AGENT_LABELS = {
    "director": "总控",
    "screenwriter": "编剧",
    "bible": "定妆",
    "storyboard": "分镜",
    "video": "视频",
    "voice": "配音",
    "qc": "质检",
}

AGENT_STAGES = {
    "director": ["ingest", "director", "script_lock", "assemble_episode", "assemble_final"],
    "screenwriter": ["screenwrite"],
    "bible": ["character_bible", "location_bible", "character_lock"],
    "storyboard": ["storyboard", "animatic"],
    "video": ["video"],
    "voice": ["tts_temp", "voice", "lipsync", "music"],
    "qc": ["shot_qc", "qc_final"],
}


def record(db, project_id: str, agent: str, content: str, *, kind: str = "note", status: str = "info", job_id: str | None = None) -> AgentEvent:
    event = AgentEvent(
        project_id=project_id,
        agent=agent,
        kind=kind,
        content=content[:2000],
        status=status,
        job_id=job_id,
    )
    db.add(event)
    db.flush()
    publish(
        project_id,
        {
            "type": "agent",
            "agent": agent,
            "kind": kind,
            "content": event.content,
            "status": status,
        },
    )
    return event


def agent_board(db, project_id: str) -> dict:
    from sqlalchemy import func

    from app.db.models import Job

    grouped: dict[str, dict[str, int]] = {}
    for stage, status, count in (
        db.query(Job.stage, Job.status, func.count(Job.id))
        .filter(Job.project_id == project_id)
        .group_by(Job.stage, Job.status)
        .all()
    ):
        grouped.setdefault(stage, {})[status] = int(count)
    agents = []
    for agent, stages in AGENT_STAGES.items():
        totals: dict[str, int] = {}
        for stage in stages:
            for status, count in grouped.get(stage, {}).items():
                totals[status] = totals.get(status, 0) + count
        total = sum(totals.values())
        if totals.get("running"):
            status = "running"
        elif totals.get("failed"):
            status = "failed"
        elif total and totals.get("succeeded") == total:
            status = "succeeded"
        elif totals.get("queued") or totals.get("awaiting_approval"):
            status = "queued"
        else:
            status = "waiting"
        agents.append({"id": agent, "label": AGENT_LABELS[agent], "status": status, "jobs": total})
    messages = (
        db.query(AgentEvent).filter(AgentEvent.project_id == project_id).order_by(AgentEvent.created_at.asc()).all()
    )
    from app.db.models import ModelCall

    calls = (
        db.query(ModelCall).filter(ModelCall.project_id == project_id).order_by(ModelCall.created_at.desc()).limit(40).all()
    )
    return {
        "agents": agents,
        "messages": [
            {
                "id": item.id,
                "agent": item.agent,
                "label": AGENT_LABELS.get(item.agent, item.agent),
                "kind": item.kind,
                "content": item.content,
                "status": item.status,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in messages
        ],
        "calls": [
            {
                "id": call.id,
                "category": call.category,
                "provider": call.provider,
                "model": call.model,
                "status": call.status,
                "latency_ms": call.latency_ms,
                "cost_usd": call.cost_usd,
                "error": call.error,
                "summary": call.request_summary or "",
                "created_at": call.created_at.isoformat() if call.created_at else None,
            }
            for call in calls
        ],
    }
