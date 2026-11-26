"""Volcengine Ark Seedream image generation.

POST {ARK_BASE_URL}/images/generations
Docs: https://www.volcengine.com/docs/82379/1541523 (image generation API).

Seedream 4.0 accepts explicit pixels and its 1K 9:16 mapping is exactly
720x1280 (921600 px, the documented floor). 4.5 and later prefer a 2K frame;
1600x2848 is the published 9:16 2K mapping. Reference images go in `image` as
URLs or data URIs. The pipeline still scales the result to the project size.
"""

import base64

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import download, send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


def seedream_size(model: str, width: int, height: int) -> str:
    if "4-0" in (model or "") or "seedream-3" in (model or ""):
        return f"{width}x{height}"
    if height >= width:
        return "1600x2848"
    return "2848x1600"


class SeedreamImage:
    provider = "seedream"

    def __init__(self, client: httpx.Client | None = None):
        settings = get_settings()
        if not settings.ark_api_key:
            raise ProviderNotConfigured("ARK_API_KEY is empty")
        self.api_key = settings.ark_api_key
        self.base_url = settings.ark_base_url.rstrip("/")
        self.default_model = settings.seedream_model
        self.client = client or httpx.Client(timeout=180)

    def generate(self, *, prompt, negative_prompt, width, height, seed, reference_images, model) -> MediaResult:
        chosen = model or self.default_model
        body: dict = {
            "model": chosen,
            "prompt": prompt,
            "size": seedream_size(chosen, width, height),
            "response_format": "b64_json",
            "watermark": False,
            "sequential_image_generation": "disabled",
        }
        if seed is not None:
            body["seed"] = int(seed)
        refs = reference_images or []
        if refs:
            body["image"] = [_data_uri(item) for item in refs[:6]]
        response = send(
            self.client,
            "POST",
            f"{self.base_url}/images/generations",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        payload = response.json()
        items = payload.get("data") or []
        if not items:
            raise ProviderResponseError("seedream response has no data")
        item = items[0]
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            data = download(self.client, item["url"])
        else:
            raise ProviderResponseError("seedream item missing image")
        return MediaResult(
            data=data,
            mime="image/png",
            usage=Usage(images=1),
            provider=self.provider,
            model=chosen,
            width=width,
            height=height,
            seed=seed,
            extra={"reference_count": len(refs), "requested_size": body["size"]},
        )


def _data_uri(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}"
