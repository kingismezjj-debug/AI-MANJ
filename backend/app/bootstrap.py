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
