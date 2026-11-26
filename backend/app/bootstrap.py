import logging
import time

from sqlalchemy import text

from app.db.base import Base
from app.db.session import engine

logger = logging.getLogger(__name__)


def wait_for_db(attempts: int = 30) -> None:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise RuntimeError(f"database not ready: {last}")


def init_database() -> None:
    wait_for_db()
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _ensure_users()


def _ensure_columns() -> None:
    """Add columns introduced after the first boot. create_all does not alter."""
    from sqlalchemy import inspect

    additions = {
        "projects": {"user_id": "VARCHAR(32)"},
        "characters": {"anchors": "TEXT"},
        "shots": {
            "video_prompt": "TEXT",
            "dialogue_timing": "TEXT",
            "revision": "INTEGER DEFAULT 0",
        },
    }
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in existing:
                continue
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _ensure_users() -> None:
    from app.config import get_settings
    from app.db.models import User
    from app.db.session import SessionLocal

    settings = get_settings()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.api_key == settings.admin_api_key).one_or_none() is None:
            db.add(User(name="管理员", role="admin", api_key=settings.admin_api_key, credit_balance=1_000_000))
        if db.query(User).filter(User.api_key == settings.dev_user_api_key).one_or_none() is None:
            db.add(
                User(
                    name="工作室",
                    role="user",
                    api_key=settings.dev_user_api_key,
                    credit_balance=float(settings.dev_user_credits or 0),
                )
            )
        db.commit()
    finally:
        db.close()


def init_storage() -> None:
    from app.storage import get_store

    last: Exception | None = None
    for _ in range(30):
        try:
            get_store().ensure_bucket()
            return
        except Exception as exc:
            last = exc
            logger.warning("object storage not ready yet: %s", exc)
            time.sleep(1)
    raise RuntimeError(f"object storage not ready: {last}")
