from fastapi import Request

from app.db.models import User


def resolve_user(db, request: Request) -> User | None:
    key = request.headers.get("x-api-key") or ""
    authorization = request.headers.get("authorization") or ""
    if not key and authorization.lower().startswith("bearer "):
        key = authorization.split(" ", 1)[1].strip()
    if not key:
        key = request.query_params.get("k") or ""
    if not key:
        return None
    return db.query(User).filter(User.api_key == key).one_or_none()
