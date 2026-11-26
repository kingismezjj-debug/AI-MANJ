import json
import logging

from app.config import get_settings

logger = logging.getLogger(__name__)


def channel(project_id: str) -> str:
    return f"zhenchuan:{project_id}"


def publish(project_id: str, event: dict) -> None:
    url = get_settings().redis_url
    if not url:
        return
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2)
        client.publish(channel(project_id), json.dumps(event, ensure_ascii=False))
    except Exception:
        logger.debug("event publish skipped", exc_info=True)
