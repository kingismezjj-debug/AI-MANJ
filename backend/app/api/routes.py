from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.registry import default_settings, preset_config
from app.api.board import build_board
from app.api.catalog import public_catalog
from app.api.costs import cost_report
from app.config import get_settings
from app.db.models import Artifact, Novel, Project
from app.db.session import get_db
from app.novel.parse import char_count, parse_bytes
from app.pipeline.controls import (
    ControlError,
    approve_all,
    approve_character,
    regenerate_character,
    regenerate_shot,
    start_run,
)
from app.pipeline.events import channel
from app.storage import get_store
from app.util import new_id

router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    title: str = "未命名项目"
    preset: str = "mock"
    provider_config: dict | None = None
    require_bible_approval: bool = False


def _project_out(project: Project) -> dict:
    return {
        "id": project.id,
        "title": project.title,
        "status": project.status,
        "phase": project.phase,
        "style": project.style,
        "aspect_ratio": project.aspect_ratio,
        "width": project.width,
        "height": project.height,
        "error": project.error,
        "settings": project.settings,
        "provider_config": project.provider_config,
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }


def _raise_control(exc: ControlError):
    raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("/health")
def health():
    return {"ok": True, "service": "zhenchuan"}


@router.get("/providers")
def providers():
    return public_catalog()


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    projects = db.query(Project).order_by(Project.created_at.desc()).all()
    payload = []
    for project in projects:
        report = cost_report(db, project)
        item = _project_out(project)
        item["costs"] = {"actual_usd": report["actual_usd"], "reference_usd": report["reference_usd"]}
        payload.append(item)
    return payload


@router.post("/projects")
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    config = body.provider_config or preset_config(body.preset)
    project = Project(
        title=body.title.strip() or "未命名项目",
        status="draft",
        phase="draft",
        provider_config=config,
        settings=default_settings(require_bible_approval=body.require_bible_approval),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _project_out(project)


@router.get("/projects/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = _load(db, project_id)
    return build_board(db, project)


@router.get("/projects/{project_id}/costs")
def project_costs(project_id: str, db: Session = Depends(get_db)):
    return cost_report(db, _load(db, project_id))


def _load(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _store_novel(db: Session, project: Project, filename: str, data: bytes) -> dict:
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "文件超过 20MB")
    try:
        text = parse_bytes(filename, data).strip()
    except Exception as exc:
        raise HTTPException(400, f"无法解析小说：{exc}") from exc
    if not text:
        raise HTTPException(400, "小说是空的")
    store = get_store()
    key = f"projects/{project.id}/novel/{new_id()}-{filename or 'novel.txt'}"
    store.put(key, data, "application/octet-stream")
    novel = db.query(Novel).filter(Novel.project_id == project.id).one_or_none()
    if novel is None:
        novel = Novel(project_id=project.id)
        db.add(novel)
    novel.filename = filename or "novel.txt"
    novel.storage_key = key
    novel.text = text
    novel.char_count = char_count(text)
    novel.analysis = {}
    db.commit()
    return {"filename": novel.filename, "char_count": novel.char_count}


@router.post("/projects/{project_id}/novel")
async def upload_novel(project_id: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    project = _load(db, project_id)
    if project.status not in {"draft", "failed"}:
        raise HTTPException(409, "项目已经开拍，不能更换小说")
    data = await file.read()
    return _store_novel(db, project, file.filename or "novel.txt", data)


@router.post("/projects/{project_id}/novel/sample")
def upload_sample(project_id: str, db: Session = Depends(get_db)):
    project = _load(db, project_id)
    if project.status not in {"draft", "failed"}:
        raise HTTPException(409, "项目已经开拍，不能更换小说")
    path = get_settings().sample_path()
    if not path.exists():
        raise HTTPException(404, "示例小说缺失")
    return _store_novel(db, project, path.name, path.read_bytes())


@router.post("/projects/{project_id}/run")
def run_project(project_id: str, db: Session = Depends(get_db)):
    project = _load(db, project_id)
    try:
        start_run(db, project)
    except ControlError as exc:
        _raise_control(exc)
    db.refresh(project)
    return _project_out(project)


@router.post("/characters/{character_id}/approve")
def approve_one(character_id: str, db: Session = Depends(get_db)):
    try:
        character = approve_character(db, character_id)
    except ControlError as exc:
        _raise_control(exc)
    return {"id": character.id, "locked": character.locked, "status": character.status}


@router.post("/projects/{project_id}/characters/approve")
def approve_cast(project_id: str, db: Session = Depends(get_db)):
    _load(db, project_id)
    try:
        count = approve_all(db, project_id)
    except ControlError as exc:
        _raise_control(exc)
    return {"approved": count}


@router.post("/characters/{character_id}/regenerate")
def regen_character(character_id: str, db: Session = Depends(get_db)):
    try:
        job = regenerate_character(db, character_id)
    except ControlError as exc:
        _raise_control(exc)
    return {"job_id": job.id, "status": job.status}


@router.post("/shots/{shot_id}/regenerate")
def regen_shot(shot_id: str, db: Session = Depends(get_db)):
    try:
        regenerate_shot(db, shot_id)
    except ControlError as exc:
        _raise_control(exc)
    return {"ok": True}


@router.get("/projects/{project_id}/events")
def project_events(project_id: str, db: Session = Depends(get_db)):
    _load(db, project_id)
    settings = get_settings()
    if not settings.redis_url:
        def empty():
            yield "event: hello\ndata: {}\n\n"

        return StreamingResponse(empty(), media_type="text/event-stream")

    def stream():
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_timeout=20)
        pubsub = client.pubsub()
        pubsub.subscribe(channel(project_id))
        yield "event: hello\ndata: {}\n\n"
        while True:
            message = pubsub.get_message(timeout=15.0)
            if message and message.get("type") == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                yield f"data: {data}\n\n"
            else:
                yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/artifacts/{artifact_id}/content")
def artifact_content(artifact_id: str, request: Request, db: Session = Depends(get_db)):
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "文件不存在")
    data = get_store().get(artifact.storage_key)
    start = 0
    end = len(data) - 1
    status = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": artifact.content_type,
        "Content-Disposition": f"inline; filename={artifact.kind}",
    }
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes=") and len(data):
        spec = range_header.split("=", 1)[1].split("-", 1)
        if spec[0]:
            start = int(spec[0])
        if len(spec) > 1 and spec[1]:
            end = int(spec[1])
        end = min(end, len(data) - 1)
        start = min(start, end)
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    chunk = data[start : end + 1]
    headers["Content-Length"] = str(len(chunk))
    return Response(content=chunk, status_code=status, headers=headers, media_type=artifact.content_type)
