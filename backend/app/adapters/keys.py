"""Several keys on one slot.

Calls walk the keys in round-robin order. A 429 or 5xx puts that key on a
short cooldown and the next key is tried. Keys are never logged whole.
"""

import time

_cursor: dict[str, int] = {}
_cooldown_until: dict[str, float] = {}


def split_keys(raw: str | None) -> list[str]:
    text = (raw or "").replace(",", "\n")
    return [part.strip() for part in text.splitlines() if part.strip()]


def cooling(key: str, now: float | None = None) -> bool:
    if not key:
        return False
    return _cooldown_until.get(key, 0) > (now if now is not None else time.time())


def mark_cooldown(key: str, seconds: float = 60) -> None:
    if key:
        _cooldown_until[key] = time.time() + seconds


def retryable_status(exc: BaseException) -> bool:
    text = str(exc)
    return any(token in text for token in ("429", "500", "502", "503", "504"))


def ordered_keys(slot_id: str, keys: list[str], now: float | None = None) -> list[str]:
    usable = [key for key in keys if not cooling(key, now)]
    if not usable:
        return []
    start = _cursor.get(slot_id, 0) % len(usable)
    _cursor[slot_id] = start + 1
    return usable[start:] + usable[:start]


def call_with_keys(slot_id: str, keys: list[str], fn):
    """Call fn(key) until one returns. Retryable errors cool that key down."""
    order = ordered_keys(slot_id, keys)
    if not order:
        raise RuntimeError("全部密钥都在冷却")
    last: Exception | None = None
    for key in order:
        try:
            return fn(key)
        except Exception as exc:
            last = exc
            if retryable_status(exc):
                mark_cooldown(key)
                continue
            raise
    assert last is not None
    raise last
