import logging
import time

from app.adapters.errors import ProviderNotConfigured
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

    def llm(self, messages, *, json_mode: bool = False, temperature: float = 0.7, model: str | None = None, max_tokens: int | None = None):
        def call(adapter, route):
            chosen = model or route.get("model") or None
            kwargs = {
                "messages": messages,
                "model": chosen,
                "temperature": temperature,
                "json_mode": json_mode,
            }
            if max_tokens and "max_tokens" in adapter.complete.__code__.co_varnames:
                kwargs["max_tokens"] = max_tokens
            return adapter.complete(**kwargs)

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

    def video(
        self,
        *,
        prompt,
        image,
        reference_images,
        duration_sec,
        seed,
        aspect_ratio,
        width,
        height,
        last_image=None,
        image_url: str | None = None,
        on_progress=None,
        resume_task_id: str | None = None,
        resume_poll_path: str | None = None,
    ):
        def call(adapter, route):
            import inspect

            kwargs = {
                "prompt": prompt,
                "image": image,
                "reference_images": reference_images or [],
                "duration_sec": duration_sec,
                "seed": seed,
                "aspect_ratio": aspect_ratio,
                "width": width,
                "height": height,
                "model": route.get("model") or None,
                "last_image": last_image,
            }
            accepted = inspect.signature(adapter.image_to_video).parameters
            if "image_url" in accepted:
                kwargs["image_url"] = image_url
            if "on_progress" in accepted:
                kwargs["on_progress"] = on_progress
            if "resume_task_id" in accepted:
                kwargs["resume_task_id"] = resume_task_id
            if "resume_poll_path" in accepted:
                kwargs["resume_poll_path"] = resume_poll_path
            return adapter.image_to_video(**kwargs)

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
        routes = list(route_chain(self.project, category, self.db))
        # Drop the read transaction before any network call. A long model
        # request must not sit idle-in-transaction.
        self.db.commit()
        for route in routes:
            keys = split_keys(route.get("api_key") or "") or [""]
            slot_id = f"{self.project.id}:{category}:{route.get('provider')}"
            note = route.get("_note") or summary
            started = time.perf_counter()

            def attempt(key: str, route=route):
                routed = dict(route)
                routed["api_key"] = key
                adapter = build_adapter(category, routed)
                return fn(adapter, routed)

            if (
                category == "video"
                and route.get("provider") == "mock"
                and last is not None
                and not isinstance(last, ProviderNotConfigured)
            ):
                break
            try:
                result = call_with_keys(slot_id, keys, attempt)
            except Exception as exc:
                logger.warning("provider %s/%s failed: %s", category, route.get("provider"), exc)
                self._record(category, route, Usage(), started, "failed", str(exc), note)
                self.db.commit()
                last = exc
                continue
            self._record(category, route, result.usage, started, "succeeded", None, note)
            self.db.commit()
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
