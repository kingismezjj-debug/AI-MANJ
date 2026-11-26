import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import resolve_user
from app.api.routes import router
from app.bootstrap import init_database, init_storage
from app.config import get_settings
from app.db.session import SessionLocal

PUBLIC_PATHS = {"/api/health", "/api/providers"}


async def auth_middleware(request, call_next):
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api") or path in PUBLIC_PATHS:
        return await call_next(request)
    db = SessionLocal()
    try:
        user = resolve_user(db, request)
    finally:
        db.close()
    if user is None:
        return JSONResponse({"detail": "需要 API Key"}, status_code=401)
    request.state.user_id = user.id
    request.state.role = user.role
    return await call_next(request)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    init_storage()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="帧川 Zhenchuan", lifespan=lifespan)
    app.middleware("http")(auth_middleware)
    settings = get_settings()
    origins = [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if origins == ["*"] else origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
