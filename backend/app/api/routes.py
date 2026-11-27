from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.pricing import forecast_novel
from app.adapters.registry import default_settings, preset_config
from app.api.board import build_board
from app.api.catalog import public_catalog
from app.api.costs import cost_report
from app.billing import hold_for_run, top_up
from app.config import get_settings
from app.db.models import Artifact, Novel, Project, User
from app.db.session import get_db
from app.novel.parse import char_count, parse_bytes
from app.pipeline.controls import (
    ControlError,
    approve_all,
    approve_character,
    approve_script,
    regenerate_character,
    regenerate_shot,
    start_run,
)
from app.pipeline.agents import agent_board
from app.pipeline.events import channel
from app.settings_store import list_slots, probe_slot, save_slots
from app.storage import get_store
from app.util import new_id

router = APIRouter(prefix="/api")


class ProjectCreate(BaseModel):
    title: str = "未命名项目"
    preset: str = "mock"
    provider_config: dict | None = None
    require_bible_approval: bool = False
    require_script_approval: bool = False
    episode_count: int | None = None
    produce_first: int | None = None
    episode_mode: str | None = None
    chapter_from: int | None = None
    chapter_to: int | None = None
    full_read: bool = False
    draft_mode: bool = False
    merge_shots: bool = False


class EstimateRequest(BaseModel):
    preset: str = "mock"
    provider_config: dict | None = None
    char_count: int = 0
    use_sample: bool = False


class RegenRequest(BaseModel):
    stage: str = "shot"
    prompt: str | None = None
    dialogue: str | None = None


class TopupRequest(BaseModel):
    user_id: str
    amount: float
    note: str = ""


class SlotBody(BaseModel):
    category: str
    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    params: dict | None = None
    enabled: bool = True


class SlotSave(BaseModel):
    slots: list[SlotBody]


class SlotTest(BaseModel):
    category: str
    provider: str = "mock"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    params: dict | None = None
    project_id: str = ""


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


@router.get("/settings/slots")
def get_slots(db: Session = Depends(get_db)):
    return list_slots(db)


@router.put("/settings/slots")
def put_slots(body: SlotSave, db: Session = Depends(get_db)):
    return save_slots(db, [item.model_dump() for item in body.slots])


@router.post("/settings/slots/test")
def post_slot_test(body: SlotTest, db: Session = Depends(get_db)):
    return probe_slot(db, body.model_dump())


@router.get("/settings/public-frames")
def public_frames(db: Session = Depends(get_db)):
    from app.pipeline.frames import frame_label, public_frame_base

    base = public_frame_base(db)
    return {"available": bool(base), "label": frame_label(db), "base": base}


@router.get("/projects/{project_id}/slots")
def project_slots(project_id: str, request: Request, db: Session = Depends(get_db)):
    _load(db, project_id, request)
    return list_slots(db, project_id)


@router.put("/projects/{project_id}/slots")
def put_project_slots(project_id: str, body: SlotSave, request: Request, db: Session = Depends(get_db)):
    _load(db, project_id, request)
    return save_slots(db, [item.model_dump() for item in body.slots], project_id=project_id)


@router.get("/projects/{project_id}/agents")
def project_agents(project_id: str, request: Request, db: Session = Depends(get_db)):
    _load(db, project_id, request)
    return agent_board(db, project_id)


@router.get("/projects")
def list_projects(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    query = db.query(Project)
    if user.role != "admin":
        query = query.filter(Project.user_id == user.id)
    projects = query.order_by(Project.created_at.desc()).all()
    from sqlalchemy import func

    from app.db.models import ModelCall

    ids = [project.id for project in projects]
    spent = {}
    if ids:
        spent = dict(
            db.query(ModelCall.project_id, func.coalesce(func.sum(ModelCall.cost_usd), 0))
            .filter(ModelCall.project_id.in_(ids), ModelCall.status == "succeeded")
            .group_by(ModelCall.project_id)
            .all()
        )
    from app.pipeline.progress import snapshot

    payload = []
    for project in projects:
        item = _project_out(project)
        item["costs"] = {"actual_usd": round(float(spent.get(project.id) or 0), 6), "reference_usd": 0}
        item["progress"] = snapshot(db, project)
        payload.append(item)
    return payload


@router.post("/projects")
def create_project(body: ProjectCreate, request: Request, db: Session = Depends(get_db)):
    config = body.provider_config or preset_config(body.preset)
    settings = default_settings(
        require_bible_approval=body.require_bible_approval,
        require_script_approval=body.require_script_approval,
    )
    if body.episode_mode:
        settings["episode_mode"] = body.episode_mode
    if body.episode_count:
        settings["episode_count"] = body.episode_count
    if body.produce_first:
        settings["produce_first"] = body.produce_first
    if body.chapter_from:
        settings["chapter_from"] = body.chapter_from
    if body.chapter_to:
        settings["chapter_to"] = body.chapter_to
    settings["full_read"] = bool(body.full_read)
    if body.draft_mode:
        settings["draft_mode"] = True
    if body.merge_shots:
        settings["merge_shots"] = True
    project = Project(
        title=body.title.strip() or "未命名项目",
        status="draft",
        phase="draft",
        user_id=_user(request, db).id,
        provider_config=config,
        settings=settings,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _project_out(project)


@router.get("/projects/{project_id}")
def get_project(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    offset: int = 0,
    limit: int = 80,
):
    project = _load(db, project_id, request)
    return build_board(db, project, offset=offset, limit=limit)


@router.get("/projects/{project_id}/costs")
def project_costs(project_id: str, request: Request, db: Session = Depends(get_db)):
    return cost_report(db, _load(db, project_id, request))


def _user(request: Request, db: Session) -> User:
    user = db.get(User, getattr(request.state, "user_id", None))
    if user is None:
        raise HTTPException(401, "需要 API Key")
    return user


def _load(db: Session, project_id: str, request: Request) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    user = _user(request, db)
    if user.role != "admin" and project.user_id not in (None, user.id):
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
async def upload_novel(project_id: str, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    if project.status not in {"draft", "failed"}:
        raise HTTPException(409, "项目已经开拍，不能更换小说")
    data = await file.read()
    return _store_novel(db, project, file.filename or "novel.txt", data)


@router.post("/projects/{project_id}/novel/sample")
def upload_sample(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    if project.status not in {"draft", "failed"}:
        raise HTTPException(409, "项目已经开拍，不能更换小说")
    path = get_settings().sample_path()
    if not path.exists():
        raise HTTPException(404, "示例小说缺失")
    return _store_novel(db, project, path.name, path.read_bytes())


@router.post("/projects/{project_id}/run")
def run_project(project_id: str, request: Request, db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    user = _user(request, db)
    novel = db.query(Novel).filter(Novel.project_id == project.id).one_or_none()
    if project.status == "draft":
        from app.settings_store import CATEGORIES, configured_slot

        config = {category: configured_slot(db, project, category) for category in CATEGORIES}
        quote = forecast_novel(config, project.settings, novel.char_count if novel else 0)
        settings = dict(project.settings or {})
        settings["cost_quote"] = quote
        project.settings = settings
        try:
            hold_for_run(db, user, project, quote["total_usd"])
            start_run(db, project)
        except ControlError as exc:
            db.rollback()
            _raise_control(exc)
    else:
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
def approve_cast(project_id: str, request: Request, db: Session = Depends(get_db)):
    _load(db, project_id, request)
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


@router.post("/projects/{project_id}/script/approve")
def approve_script_route(project_id: str, request: Request, db: Session = Depends(get_db)):
    _load(db, project_id, request)
    try:
        approve_script(db, project_id)
    except ControlError as exc:
        _raise_control(exc)
    return {"ok": True}


@router.post("/shots/{shot_id}/regenerate")
def regen_shot(shot_id: str, body: RegenRequest | None = None, db: Session = Depends(get_db)):
    payload = body or RegenRequest()
    try:
        regenerate_shot(db, shot_id, stage=payload.stage, prompt=payload.prompt, dialogue=payload.dialogue)
    except ControlError as exc:
        _raise_control(exc)
    return {"ok": True}


@router.post("/estimates")
def estimates(body: EstimateRequest):
    config = body.provider_config or preset_config(body.preset)
    char_count = body.char_count
    if body.use_sample:
        path = get_settings().sample_path()
        if path.exists():
            char_count = len("".join(path.read_text(encoding="utf-8").split()))
    return forecast_novel(config, default_settings(), char_count)


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, request: Request, format: str = "jianying", db: Session = Depends(get_db)):
    project = _load(db, project_id, request)
    if format not in {"jianying", "editorial"}:
        raise HTTPException(400, "format 只能是 jianying 或 editorial")
    from app.export.pack import build_zip

    payload = build_zip(db, project, get_store(), "jianying" if format == "jianying" else "editorial")
    filename = "zhenchuan-jianying.zip" if format == "jianying" else "zhenchuan-editorial.zip"
    return Response(content=payload, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    return {"id": user.id, "name": user.name, "role": user.role, "credit_balance": user.credit_balance}


@router.get("/admin/users")
def admin_users(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "需要管理员")
    return [
        {"id": item.id, "name": item.name, "role": item.role, "credit_balance": item.credit_balance}
        for item in db.query(User).order_by(User.created_at).all()
    ]


@router.post("/admin/topup")
def admin_topup(body: TopupRequest, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "需要管理员")
    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    try:
        topped = top_up(db, target, body.amount, body.note)
    except ControlError as exc:
        _raise_control(exc)
    return {"id": topped.id, "credit_balance": topped.credit_balance}


@router.get("/projects/{project_id}/events")
def project_events(project_id: str, request: Request, db: Session = Depends(get_db)):
    _load(db, project_id, request)
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
