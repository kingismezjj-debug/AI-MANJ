"""Book bible, assets, script edits, relay setup, draft upscale, stuck jobs."""

import io

from fastapi import APIRouter, Depends, HTTPException, Request
from PIL import Image
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.router import ModelRouter
from app.db.models import Asset, AssetVersion, Episode, Job, Novel, Shot
from app.db.session import get_db
from app.pipeline.artifacts import save_bytes
from app.pipeline.assets import edit_asset, generate_views, import_card, rollback_asset
from app.pipeline.controls import ControlError, regenerate_shot
from app.pipeline.edits import affected_report, apply_hunks, hunks, mark_downstream
from app.pipeline.longform import continue_episodes
from app.pipeline.schema import extract_json
from app.pipeline.stuck import list_stuck, reap
from app.settings_store import apply_relay, fetch_model_ids
from app.storage import get_store

router = APIRouter(prefix="/api")


class RelayBody(BaseModel):
    base_url: str
    api_key: str
    fetch_models: bool = False


class ContinueBody(BaseModel):
    count: int = 1


class RewriteBody(BaseModel):
    shot_id: str
    field: str = "description"
    instruction: str
    text: str = ""


class AcceptBody(BaseModel):
    shot_id: str
    field: str = "description"
    before: str
    proposed: str
    hunks: list[int]


class ShotEdit(BaseModel):
    description: str | None = None
    dialogue: str | None = None
    visual_prompt: str | None = None


class AssetEdit(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    kind: str = "prop"


def _load(db, project_id, request):
    from app.api.routes import _load as load

    return load(db, project_id, request)


def _user(request, db):
    from app.api.routes import _user as user

    return user(request, db)


@router.post("/settings/relay")
def relay(body: RelayBody, db: Session = Depends(get_db)):
    names = None
    note = ""
    if body.fetch_models:
        try:
            names = fetch_model_ids(body.base_url, body.api_key)
        except Exception as exc:
            note = f"模型列表没有取到，已用推荐名。{exc}"[:200]
    saved = apply_relay(db, body.base_url, body.api_key, names)
    saved["note"] = note or "三条接口已写成 OpenAI 兼容。"
    return saved


@router.get("/projects/{project_id}/book")
def book(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    novel = db.query(Novel).filter(Novel.project_id == project.id).one_or_none()
    analysis = (novel.analysis if novel else None) or {}
    episodes = db.query(Episode).filter(Episode.project_id == project.id).order_by(Episode.number).all()
    shots = db.query(Shot).filter(Shot.project_id == project.id).all()
    return {
        "bible": analysis.get("bible") or {
            "characters": analysis.get("characters") or [],
            "world": analysis.get("tone") or "",
            "timeline": [analysis.get("summary") or ""],
        },
        "title": analysis.get("title") or project.title,
        "summary": analysis.get("summary") or "",
        "chapters": len(analysis.get("bible", {}).get("timeline") or []) or 1,
        "episode_count": (project.settings or {}).get("episode_count") or 60,
        "produce_first": (project.settings or {}).get("produce_first") or 2,
        "episodes": [
            {
                "id": episode.id,
                "number": episode.number,
                "title": episode.title,
                "synopsis": episode.synopsis,
                "status": episode.status,
                "shots": len([shot for shot in shots if shot.episode_id == episode.id]),
            }
            for episode in episodes
        ],
    }


@router.post("/projects/{project_id}/continue")
def continue_run(project_id: str, body: ContinueBody, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    ids = continue_episodes(db, project, body.count)
    return {"jobs": ids}


@router.get("/projects/{project_id}/script")
def script_view(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    episodes = db.query(Episode).filter(Episode.project_id == project.id).order_by(Episode.number).all()
    shots = db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.number).all()
    return {
        "episodes": [
            {
                "id": episode.id,
                "number": episode.number,
                "title": episode.title,
                "status": episode.status,
                "shots": [
                    {
                        "id": shot.id,
                        "number": shot.number,
                        "description": shot.description,
                        "dialogue": shot.dialogue,
                        "speaker": shot.speaker,
                        "visual_prompt": shot.visual_prompt,
                        "stale": bool(shot.stale),
                    }
                    for shot in shots
                    if shot.episode_id == episode.id
                ],
            }
            for episode in episodes
        ]
    }


@router.put("/shots/{shot_id}")
def edit_shot(shot_id: str, body: ShotEdit, db: Session = Depends(get_db)):
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(404, "镜头不存在")
    if body.description is not None:
        shot.description = body.description
    if body.dialogue is not None:
        shot.dialogue = body.dialogue
    if body.visual_prompt is not None:
        shot.visual_prompt = body.visual_prompt
    affected = mark_downstream(db, shot)
    db.commit()
    return {"ok": True, "affected": affected_report(db, shot), "stale": affected}


@router.post("/projects/{project_id}/rewrite")
def rewrite(project_id: str, body: RewriteBody, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    shot = db.get(Shot, body.shot_id)
    if shot is None or shot.project_id != project.id:
        raise HTTPException(404, "镜头不存在")
    source = body.text or getattr(shot, body.field, "") or ""
    result = ModelRouter(db, project).llm(
        [
            {"role": "system", "content": "TASK:REWRITE\n按要求改写，只输出 JSON：{\"text\": \"...\"}"},
            {"role": "user", "content": f"要求：{body.instruction}\n<<<NOVEL\n{source}\n>>>"},
        ],
        json_mode=True,
        temperature=0.4,
    )
    try:
        proposed = extract_json(result.text).get("text") or result.text
    except Exception:
        proposed = result.text
    return {
        "before": source,
        "proposed": proposed,
        "hunks": hunks(source, proposed),
        "affected": affected_report(db, shot),
    }


@router.post("/projects/{project_id}/rewrite/accept")
def accept_rewrite(project_id: str, body: AcceptBody, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    shot = db.get(Shot, body.shot_id)
    if shot is None or shot.project_id != project.id:
        raise HTTPException(404, "镜头不存在")
    if body.field not in {"description", "dialogue", "visual_prompt"}:
        raise HTTPException(400, "不能改这个字段")
    merged = apply_hunks(body.before, body.proposed, body.hunks)
    setattr(shot, body.field, merged)
    affected = mark_downstream(db, shot)
    db.commit()
    return {"text": merged, "stale": affected, "affected": affected_report(db, shot)}


@router.get("/projects/{project_id}/assets")
def project_assets(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    rows = db.query(Asset).filter(Asset.project_id == project.id).order_by(Asset.kind, Asset.name).all()
    return {"assets": [_asset_out(db, row) for row in rows]}


@router.get("/library/assets")
def library(db: Session = Depends(get_db)):
    rows = db.query(Asset).filter(Asset.project_id == "").order_by(Asset.name).all()
    return {"assets": [_asset_out(db, row) for row in rows]}


@router.post("/library/assets")
def library_add(body: AssetEdit, db: Session = Depends(get_db)):
    row = Asset(project_id="", kind=body.kind, name=body.name or "未命名", description=body.description or "", prompt=body.prompt or "", version=1, meta={"views": {}})
    db.add(row)
    db.commit()
    return _asset_out(db, row)


@router.post("/projects/{project_id}/assets/import")
def import_asset(project_id: str, body: dict, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    source = db.get(Asset, body.get("asset_id"))
    if source is None:
        raise HTTPException(404, "资产不存在")
    copy = import_card(db, project.id, source)
    db.commit()
    return _asset_out(db, copy)


@router.post("/assets/{asset_id}")
def update_asset(asset_id: str, body: AssetEdit, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    stale = edit_asset(db, asset, name=body.name, description=body.description, prompt=body.prompt)
    db.commit()
    return {"asset": _asset_out(db, asset), "stale_shots": stale}


@router.post("/assets/{asset_id}/rollback")
def rollback(asset_id: str, body: dict, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    try:
        rollback_asset(db, asset, int(body.get("version") or 1))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    db.commit()
    return _asset_out(db, asset)


@router.post("/assets/{asset_id}/views")
def views(asset_id: str, request: Request, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None or not asset.project_id:
        raise HTTPException(404, "资产不存在")
    project = _load(db, asset.project_id, request)
    generate_views(db, project, asset, ModelRouter(db, project), get_store(), save_bytes)
    db.commit()
    return _asset_out(db, asset)


@router.post("/assets/{asset_id}/regenerate")
def regen_bound(asset_id: str, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "资产不存在")
    stale = edit_asset(db, asset)
    queued = []
    for shot_id in stale:
        try:
            regenerate_shot(db, shot_id, stage="shot")
            queued.append(shot_id)
        except ControlError:
            continue
    db.commit()
    return {"stale_shots": stale, "queued": queued}


@router.post("/projects/{project_id}/grid")
def grid(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    from app.db.models import Artifact

    shots = db.query(Shot).filter(Shot.project_id == project.id).order_by(Shot.number).all()
    frames = []
    for shot in shots:
        art = (
            db.query(Artifact)
            .filter(Artifact.entity_id == shot.id, Artifact.kind == "keyframe")
            .order_by(Artifact.created_at.desc())
            .first()
        )
        if art is not None:
            frames.append(get_store().get(art.storage_key))
    if not frames:
        raise HTTPException(409, "还没有分镜")
    n = 2 if len(frames) <= 4 else 3
    cell = 240
    canvas = Image.new("RGB", (cell * n, cell * n), (24, 20, 16))
    for index, blob in enumerate(frames[: n * n]):
        tile = Image.open(io.BytesIO(blob)).convert("RGB").resize((cell, cell))
        canvas.paste(tile, ((index % n) * cell, (index // n) * cell))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    artifact = save_bytes(
        db,
        get_store(),
        project_id=project.id,
        entity_type="project",
        entity_id=project.id,
        kind="storyboard_grid",
        data=buf.getvalue(),
        content_type="image/png",
        job_id=None,
        meta={"cells": min(len(frames), n * n)},
    )
    db.commit()
    return {"url": f"/api/artifacts/{artifact.id}/content", "cells": min(len(frames), n * n)}


@router.post("/projects/{project_id}/grid/confirm")
def confirm_grid(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    settings = dict(project.settings or {})
    settings["grid_confirmed"] = True
    project.settings = settings
    db.commit()
    return {"ok": True}


@router.post("/shots/{shot_id}/upscale")
def upscale_shot(shot_id: str, db: Session = Depends(get_db)):
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise HTTPException(404, "镜头不存在")
    shot.quality = "final"
    try:
        regenerate_shot(db, shot_id, stage="video")
    except ControlError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    db.commit()
    return {"ok": True, "quality": "final"}


@router.post("/episodes/{episode_id}/upscale")
def upscale_episode(episode_id: str, db: Session = Depends(get_db)):
    shots = db.query(Shot).filter(Shot.episode_id == episode_id).all()
    done = []
    for shot in shots:
        shot.quality = "final"
        try:
            regenerate_shot(db, shot.id, stage="video")
            done.append(shot.id)
        except ControlError:
            continue
    db.commit()
    return {"shots": done}


@router.get("/admin/stuck")
def admin_stuck(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "需要管理员")
    return {"jobs": list_stuck(db)}


@router.post("/admin/stuck/reap")
def admin_reap(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "需要管理员")
    return {"jobs": reap(db)}


def _asset_out(db, asset: Asset) -> dict:
    versions = (
        db.query(AssetVersion).filter(AssetVersion.asset_id == asset.id).order_by(AssetVersion.number).all()
    )
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "kind": asset.kind,
        "name": asset.name,
        "description": asset.description,
        "prompt": asset.prompt,
        "version": asset.version,
        "status": asset.status,
        "source_id": asset.source_id,
        "views": (asset.meta or {}).get("views") or {},
        "versions": [{"number": item.number, "note": item.note} for item in versions],
    }
