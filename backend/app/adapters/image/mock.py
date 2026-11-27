from app.adapters.types import MediaResult, Usage
from app.media.images import render_card


class MockImage:
    provider = "mock"

    def generate(self, *, prompt, negative_prompt, width, height, seed, reference_images, model) -> MediaResult:
        if "定妆" in prompt:
            title = "定妆"
        elif "场景" in prompt:
            title = "场景"
        else:
            title = "分镜"
        data = render_card(
            width=width,
            height=height,
            title=title,
            subtitle=f"refs {len(reference_images or [])}",
            body=prompt,
            seed=int(seed or 1),
            badge="MOCK 2D ANIME",
        )
        return MediaResult(
            data=data,
            mime="image/png",
            usage=Usage(images=1),
            provider=self.provider,
            model=model or "mock-image",
            width=width,
            height=height,
            seed=seed,
            extra={"reference_count": len(reference_images or [])},
        )
