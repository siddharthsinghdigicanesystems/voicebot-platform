"""Audio helper tests."""

from __future__ import annotations

from app.audio import (
    MULAW_FRAME_BYTES,
    chunk_mulaw,
    estimate_mulaw_duration_seconds,
    mulaw_silence_frame,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)


def test_silence_frame_is_correct_size_and_byte() -> None:
    frame = mulaw_silence_frame()
    assert len(frame) == MULAW_FRAME_BYTES
    assert all(b == 0xFF for b in frame)


def test_chunking_pads_final_frame() -> None:
    audio = b"\x80" * (MULAW_FRAME_BYTES + 50)
    chunks = list(chunk_mulaw(audio))
    assert len(chunks) == 2
    assert all(len(c) == MULAW_FRAME_BYTES for c in chunks)
    # Last chunk: 50 bytes of payload, then μ-law silence padding.
    last = chunks[-1]
    assert last[:50] == b"\x80" * 50
    assert last[50:] == bytes([0xFF]) * (MULAW_FRAME_BYTES - 50)


def test_chunking_exact_multiple() -> None:
    audio = b"\x80" * (MULAW_FRAME_BYTES * 3)
    chunks = list(chunk_mulaw(audio))
    assert len(chunks) == 3
    assert all(len(c) == MULAW_FRAME_BYTES for c in chunks)


def test_round_trip_pcm_mulaw() -> None:
    pcm = (b"\x00\x10" * 80) + (b"\xff\xef" * 80)  # arbitrary signal
    encoded = pcm16_to_mulaw(pcm)
    assert len(encoded) == 160  # 1 byte μ-law per 2 bytes PCM
    decoded = mulaw_to_pcm16(encoded)
    assert len(decoded) == len(pcm)


def test_duration_estimate() -> None:
    one_second = b"\x00" * 8000
    assert abs(estimate_mulaw_duration_seconds(one_second) - 1.0) < 1e-6
