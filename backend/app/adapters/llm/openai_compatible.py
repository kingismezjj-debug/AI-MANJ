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
        if not settings.openai_api_key:
            raise ProviderNotConfigured("OPENAI_API_KEY is empty")
        self.api_key = settings.openai_api_key
        self.base_url = settings.openai_base_url.rstrip("/")
        self.default_model = settings.openai_model
        self.client = client or httpx.Client(timeout=120)

    def complete(self, *, messages, model, temperature, json_mode) -> LLMResult:
        chosen = model or self.default_model
        body = {"model": chosen, "messages": messages, "temperature": temperature}
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
