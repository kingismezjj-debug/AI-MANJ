import logging
import time

from app.adapters.keys import call_with_keys, split_keys
from app.adapters.pricing import cost_of
from app.adapters.registry import build_adapter, route_chain
from app.adapters.types import Usage
from app.db.models import ModelCall

logger = logging.getLogger(__name__)


class ModelRouter:
    """Routes one logical call across the project's provider chain.

    Every attempt, including failures, is written to model_calls so the project
    page can show a cost estimate from real metering rather than a guess.
    """

    def __init__(self, db, project, job_id: str | None = None):
        self.db = db
        self.project = project
        self.job_id = job_id

    def llm(self, messages, *, json_mode: bool = False, temperature: float = 0.7):
        def call(adapter, route):
            return adapter.complete(
                messages=messages,
                model=route.get("model") or None,
                temperature=temperature,
                json_mode=json_mode,
            )

        return self._invoke("llm", call, summary="编剧/总控")

    def image(self, *, prompt, negative_prompt="", width, height, seed, reference_images=None):
        def call(adapter, route):
            return adapter.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                seed=seed,
                reference_images=reference_images or [],
                model=route.get("model") or None,
            )

        return self._invoke("image", call, summary=(prompt or "")[:80])

    def video(self, *, prompt, image, reference_images, duration_sec, seed, aspect_ratio, width, height, last_image=None):
        def call(adapter, route):
            return adapter.image_to_video(
                prompt=prompt,
                image=image,
                reference_images=reference_images or [],
                duration_sec=duration_sec,
                seed=seed,
                aspect_ratio=aspect_ratio,
                width=width,
                height=height,
                model=route.get("model") or None,
                last_image=last_image,
            )

        return self._invoke("video", call, summary=(prompt or "")[:80])

    def tts(self, *, text, voice, speed=1.0):
        def call(adapter, route):
            return adapter.synthesize(text=text, voice=voice, speed=speed, model=route.get("model") or None)

        return self._invoke("tts", call, summary=(text or "")[:80])

    def lipsync(self, *, video, audio, video_url=None, audio_url=None):
        def call(adapter, route):
            return adapter.sync(
                video=video,
                audio=audio,
                video_url=video_url,
                audio_url=audio_url,
                model=route.get("model") or None,
            )

        return self._invoke("lipsync", call, summary="口型")

    def music(self, *, prompt, duration_sec):
        def call(adapter, route):
            return adapter.generate(prompt=prompt, duration_sec=duration_sec, model=route.get("model") or None)

        return self._invoke("music", call, summary=(prompt or "")[:80])

    def _invoke(self, category: str, fn, summary: str = ""):
        last: Exception | None = None
        for route in route_chain(self.project, category, self.db):
            keys = split_keys(route.get("api_key") or "") or [""]
            slot_id = f"{self.project.id}:{category}:{route.get('provider')}"
            note = route.get("_note") or summary
            started = time.perf_counter()

            def attempt(key: str, route=route):
                routed = dict(route)
                routed["api_key"] = key
                adapter = build_adapter(category, routed)
                return fn(adapter, routed)

            try:
                result = call_with_keys(slot_id, keys, attempt)
            except Exception as exc:
                logger.warning("provider %s/%s failed: %s", category, route.get("provider"), exc)
                self._record(category, route, Usage(), started, "failed", str(exc), note)
                last = exc
                continue
            self._record(category, route, result.usage, started, "succeeded", None, note)
            return result
        assert last is not None
        raise last

    def _record(self, category, route, usage: Usage, started: float, status: str, error: str | None, summary: str):
        latency_ms = int((time.perf_counter() - started) * 1000)
        cost = cost_of(category, route.get("provider") or "", usage) if status == "succeeded" else 0.0
        self.db.add(
            ModelCall(
                project_id=self.project.id,
                job_id=self.job_id,
                category=category,
                provider=route.get("provider") or "",
                model=route.get("model") or "",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                images=usage.images,
                video_seconds=usage.video_seconds,
                audio_chars=usage.audio_chars,
                lipsync_seconds=usage.lipsync_seconds,
                cost_usd=round(cost, 6),
                latency_ms=latency_ms,
                status=status,
                error=(error or "")[:1000] or None,
                request_summary=(summary or "")[:300],
            )
        )
        self.db.flush()
