"""OpenAI-compatible image generation.

POST {OPENAI_BASE_URL}/images/generations
DALL·E 3 sizes are limited; vertical uses 1024x1792 and the pipeline scales
the bytes to the project frame. Reference images are described in the prompt
because the generations endpoint does not take a reference array.
"""

import base64

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import download, send
from app.adapters.types import MediaResult, Usage
from app.config import get_settings


class OpenAICompatibleImage:
    provider = "openai_compatible"

    def __init__(self, client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = settings.openai_image_model
        self.client = client or httpx.Client(timeout=180)

    def apply_route(self, route: dict) -> None:
        from app.settings_store import normalize_openai_base

        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = normalize_openai_base(route["base_url"])
        if route.get("model"):
            self.default_model = route["model"]

    def generate(self, *, prompt, negative_prompt, width, height, seed, reference_images, model) -> MediaResult:
        if not self.api_key:
            raise ProviderNotConfigured("image API key is empty")
        chosen = model or self.default_model
        size = "1024x1792" if height >= width else "1792x1024"
        note = ""
        if reference_images:
            note = f" Keep the same character identity across {len(reference_images)} reference sheets."
        body = {
            "model": chosen,
            "prompt": (prompt + note)[:3800],
            "size": size,
            "response_format": "b64_json",
            "n": 1,
        }
        response = send(
            self.client,
            "POST",
            f"{self.base_url}/images/generations",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        payload = response.json()
        item = (payload.get("data") or [None])[0]
        if not item:
            raise ProviderResponseError("image response has no data")
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            data = download(self.client, item["url"])
        else:
            raise ProviderResponseError("image item missing payload")
        return MediaResult(
            data=data,
            mime="image/png",
            usage=Usage(images=1),
            provider=self.provider,
            model=chosen,
            width=width,
            height=height,
            seed=seed,
            extra={"reference_count": len(reference_images or []), "requested_size": size},
        )
