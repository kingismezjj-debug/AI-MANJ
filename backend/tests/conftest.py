import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="zc-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{ROOT / 'test.db'}"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_DIR"] = str(ROOT / "store")
os.environ["REDIS_URL"] = ""
os.environ["INLINE_JOBS"] = "0"
os.environ["SAMPLE_NOVEL_PATH"] = str(Path(__file__).resolve().parents[2] / "samples" / "short_novel.txt")
os.environ["OPENAI_API_KEY"] = ""
os.environ["ARK_API_KEY"] = ""
os.environ["KLING_API_KEY"] = ""
os.environ["KLING_ACCESS_KEY"] = ""
os.environ["KLING_SECRET_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["ELEVENLABS_API_KEY"] = ""

from app.config import get_settings

get_settings.cache_clear()

import pytest
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.pipeline.dispatch import set_enqueue_override


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def capture_enqueue():
    queued: list[str] = []
    set_enqueue_override(queued.append)
    yield queued
    set_enqueue_override(None)


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers["X-API-Key"] = "zc-dev-admin"
        yield test_client
