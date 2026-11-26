from dataclasses import dataclass, field


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    images: int = 0
    video_seconds: float = 0.0
    audio_chars: int = 0
    lipsync_seconds: float = 0.0


@dataclass
class LLMResult:
    text: str
    usage: Usage
    provider: str
    model: str


@dataclass
class MediaResult:
    data: bytes
    mime: str
    usage: Usage
    provider: str
    model: str
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    seed: int | None = None
    extra: dict = field(default_factory=dict)
