import time

import jwt

from app.adapters.errors import ProviderNotConfigured


def kling_headers(settings) -> dict:
    if settings.kling_api_key:
        token = settings.kling_api_key
    elif settings.kling_access_key and settings.kling_secret_key:
        now = int(time.time())
        token = jwt.encode(
            {"iss": settings.kling_access_key, "exp": now + 1800, "nbf": now - 5},
            settings.kling_secret_key,
            algorithm="HS256",
            headers={"alg": "HS256", "typ": "JWT"},
        )
    else:
        raise ProviderNotConfigured("Set KLING_API_KEY or KLING_ACCESS_KEY plus KLING_SECRET_KEY")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
