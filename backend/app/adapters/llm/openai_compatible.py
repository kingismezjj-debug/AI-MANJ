"""Chat completions against any OpenAI-compatible base URL.

DeepSeek, an internal relay, and OpenAI itself all speak this shape:
POST {base_url}/chat/completions with a bearer key. base_url should include the
/v1 prefix when the vendor uses one.
"""

import httpx

from app.adapters.errors import ProviderNotConfigured, ProviderResponseError
from app.adapters.http import send
from app.adapters.types import LLMResult, Usage
from app.config import get_settings


class OpenAICompatibleLLM:
    provider = "openai_compatible"

    def __init__(self, client: httpx.Client | None = None):
        settings = get_settings()
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = settings.openai_model
        self.client = client or httpx.Client(timeout=120)

    def apply_route(self, route: dict) -> None:
        from app.settings_store import normalize_openai_base

        if route.get("api_key"):
            self.api_key = route["api_key"]
        if route.get("base_url"):
            self.base_url = normalize_openai_base(route["base_url"])
        if route.get("model"):
            self.default_model = route["model"]

    def complete(self, *, messages, model, temperature, json_mode, max_tokens: int | None = None) -> LLMResult:
        if not self.api_key:
            raise ProviderNotConfigured("LLM API key is empty")
        chosen = model or self.default_model
        body = {"model": chosen, "messages": messages, "temperature": temperature}
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            payload = self._post(body)
        except ProviderResponseError:
            if not json_mode:
                raise
            body.pop("response_format", None)
            payload = self._post(body)
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError("chat completion missing message content") from exc
        usage_raw = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens") or 0),
            output_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        return LLMResult(text=text or "", usage=usage, provider=self.provider, model=chosen)

    def _post(self, body: dict) -> dict:
        response = send(
            self.client,
            "POST",
            f"{self.base_url}/chat/completions",
            idempotent=False,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        return response.json()
