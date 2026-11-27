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
    _ensure_indexes()
    _ensure_users()


def _ensure_indexes() -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_jobs_project_status ON jobs (project_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_project_updated ON jobs (project_id, updated_at)",
        "CREATE INDEX IF NOT EXISTS ix_artifacts_project_entity ON artifacts (project_id, entity_id)",
        "CREATE INDEX IF NOT EXISTS ix_shots_project_episode ON shots (project_id, episode_id)",
    )
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


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
            "stale": "BOOLEAN DEFAULT FALSE",
            "quality": "VARCHAR(16) DEFAULT 'final'",
        },
        "model_calls": {"request_summary": "TEXT DEFAULT ''"},
        "jobs": {"heartbeat_at": "TIMESTAMP WITH TIME ZONE"},
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
        if engine.dialect.name == "postgresql" and "shots" in existing:
            stale = next((column for column in inspector.get_columns("shots") if column["name"] == "stale"), None)
            if stale is not None and "INT" in str(stale["type"]).upper():
                conn.execute(text("ALTER TABLE shots ALTER COLUMN stale DROP DEFAULT"))
                conn.execute(text("ALTER TABLE shots ALTER COLUMN stale TYPE BOOLEAN USING CASE WHEN stale = 0 THEN FALSE ELSE TRUE END"))
                conn.execute(text("ALTER TABLE shots ALTER COLUMN stale SET DEFAULT FALSE"))


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
        from app.settings_store import ensure_defaults

        ensure_defaults(db)
        from app.pipeline.plan import compact_overgrown

        compact_overgrown(db)
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
