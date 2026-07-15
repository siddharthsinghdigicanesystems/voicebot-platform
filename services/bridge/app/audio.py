"""Audio helpers.

We deliberately keep the audio path on the wire as g711 μ-law @ 8 kHz from
Tata all the way through to OpenAI Realtime — it's a supported format on
both ends and skipping resampling saves real latency and CPU.

The functions here are only used for:
  - chunking outbound audio into frames of the right size
  - μ-law silence generation (used to flush jitter buffers on barge-in)
  - μ-law encode/decode for tests and recordings
"""

from __future__ import annotations

import audioop  # noqa: A005  (stdlib in py < 3.13; replaced with audioop-lts after that)
from collections.abc import Iterator

# 20 ms frame at 8 kHz, 1 byte per sample for μ-law
MULAW_FRAME_SAMPLES = 160
MULAW_FRAME_BYTES = 160
MULAW_SILENCE_BYTE = 0xFF  # μ-law silence is 0xFF (not 0x00 like PCM)


def mulaw_silence_frame() -> bytes:
    """A single 20 ms frame of μ-law silence."""
    return bytes([MULAW_SILENCE_BYTE]) * MULAW_FRAME_BYTES


def chunk_mulaw(data: bytes, frame_bytes: int = MULAW_FRAME_BYTES) -> Iterator[bytes]:
    """Yield `frame_bytes`-sized chunks of μ-law audio.

    The final chunk is padded with μ-law silence to `frame_bytes` so we never
    send a runt frame to the carrier (some carriers drop them).
    """
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    for i in range(0, len(data), frame_bytes):
        chunk = data[i : i + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + bytes([MULAW_SILENCE_BYTE]) * (frame_bytes - len(chunk))
        yield chunk


def pcm16_to_mulaw(pcm: bytes) -> bytes:
    """Convert little-endian PCM16 (16-bit signed) to μ-law. Used in tests."""
    return audioop.lin2ulaw(pcm, 2)


def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    """Convert μ-law to little-endian PCM16. Used for recordings/playback."""
    return audioop.ulaw2lin(mulaw, 2)


def estimate_mulaw_duration_seconds(mulaw: bytes, sample_rate: int = 8000) -> float:
    """Duration in seconds of a μ-law byte stream at the given sample rate."""
    return len(mulaw) / sample_rate
