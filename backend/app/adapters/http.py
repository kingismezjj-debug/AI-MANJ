import time

import httpx

from app.adapters.errors import ProviderResponseError, TransientProviderError


def send(client: httpx.Client, method: str, url: str, *, idempotent: bool = False, **kwargs) -> httpx.Response:
    """POST is retried only on connection errors and HTTP 429.

    A 5xx on create can mean the vendor already billed a task, so those are not
    retried. GET/poll calls pass idempotent=True and retry 5xx as well.
    """
    last: Exception | None = None
    for attempt in range(3):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            last = TransientProviderError(str(exc))
            time.sleep(0.05 * (2**attempt))
            continue
        code = response.status_code
        if code == 429 or (idempotent and code >= 500):
            last = TransientProviderError(f"{code} {response.text[:300]}")
            time.sleep(0.05 * (2**attempt))
            continue
        if code >= 400:
            raise ProviderResponseError(f"{code} {response.text[:500]}")
        return response
    assert last is not None
    raise last


def download(client: httpx.Client, url: str) -> bytes:
    response = send(client, "GET", url, idempotent=True, timeout=120)
    return response.content
