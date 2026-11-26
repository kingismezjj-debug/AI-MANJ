import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Artifact
from app.util import new_id

EXTENSIONS = {
    "image/png": "png",
    "video/mp4": "mp4",
    "audio/wav": "wav",
    "audio/mpeg": "mp3",
    "application/json": "json",
    "text/plain": "txt",
    "text/x-ssa": "ass",
}


def save_bytes(
    db: Session,
    store,
    *,
    project_id: str,
    entity_type: str,
    entity_id: str,
    kind: str,
    data: bytes,
    content_type: str,
    job_id: str | None,
    meta: dict | None = None,
) -> Artifact:
    artifact_id = new_id()
    ext = EXTENSIONS.get(content_type, "bin")
    key = f"projects/{project_id}/{kind}/{artifact_id}.{ext}"
    store.put(key, data, content_type)
    artifact = Artifact(
        id=artifact_id,
        project_id=project_id,
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        storage_key=key,
        content_type=content_type,
        byte_size=len(data),
        meta=meta or {},
        job_id=job_id,
    )
    db.add(artifact)
    db.flush()
    return artifact


def latest_artifact(db: Session, entity_id: str, kind: str) -> Artifact | None:
    return (
        db.query(Artifact)
        .filter(Artifact.entity_id == entity_id, Artifact.kind == kind)
        .order_by(Artifact.created_at.desc())
        .first()
    )


def materialize(store, artifact: Artifact, folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(store.get(artifact.storage_key))
    return path


def temp_workspace():
    return tempfile.TemporaryDirectory(prefix="zc-stage-")
