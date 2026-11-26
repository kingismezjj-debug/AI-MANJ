import io
import math
import struct
import wave


def synth_wav(duration: float, freq: float = 220.0, sample_rate: int = 22050, volume: float = 0.25) -> bytes:
    """Local stand-in for TTS and music. No network, no codec dependency."""
    duration = max(0.3, float(duration))
    count = int(duration * sample_rate)
    amplitude = int(32767 * max(0.02, min(volume, 0.9)))
    frames = bytearray()
    for i in range(count):
        t = i / sample_rate
        attack = min(1.0, t / 0.04)
        release = min(1.0, max(0.0, (duration - t) / 0.08))
        env = attack * release
        wobble = 1 + 0.015 * math.sin(2 * math.pi * 4 * t)
        sample = int(env * amplitude * math.sin(2 * math.pi * freq * wobble * t))
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)
    return buf.getvalue()


def synth_pad(duration: float) -> bytes:
    """A quiet two-note bed mixed in Python so the final cut is not silent."""
    duration = max(1.0, float(duration))
    sample_rate = 22050
    count = int(duration * sample_rate)
    frames = bytearray()
    for i in range(count):
        t = i / sample_rate
        env = 0.7 + 0.3 * math.sin(2 * math.pi * 0.08 * t)
        tone = 0.6 * math.sin(2 * math.pi * 110 * t) + 0.4 * math.sin(2 * math.pi * 164.8 * t)
        sample = int(max(-1, min(1, tone * env * 0.18)) * 32767)
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)
    return buf.getvalue()
