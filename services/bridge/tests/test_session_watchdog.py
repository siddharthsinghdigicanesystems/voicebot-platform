"""Watchdog tests.

We exercise `Session._pump_watchdog` against a fake telephony adapter and
synthetic clock state. The pump itself is a pure function of `time.monotonic`
plus session state, so we drive it via low timeouts and `monkeypatch`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.session import Session
from app.telephony.base import TelephonyAdapter


class _FakeAdapter(TelephonyAdapter):
    """Minimal adapter that records hangup() calls."""

    def __init__(self) -> None:
        self.hangup_called = False

    async def receive_call(self):  # type: ignore[override]
        raise NotImplementedError

    def receive_audio(self):  # type: ignore[override]
        async def _empty():
            if False:
                yield b""

        return _empty()

    async def send_audio(self, mulaw_frame: bytes) -> None:
        return

    async def send_mark(self, mark: str) -> None:
        return

    async def clear_buffer(self) -> None:
        return

    async def transfer(self, destination: str) -> None:
        return

    async def hangup(self) -> None:
        self.hangup_called = True

    async def close(self) -> None:
        return


def _make_session() -> tuple[Session, _FakeAdapter]:
    adapter = _FakeAdapter()
    s = Session(adapter)
    # The real run() opens an OpenAI WS; for these unit tests we never call run().
    # We invoke _pump_watchdog directly with prefilled session state.
    s.openai = AsyncMock()  # type: ignore[assignment]
    return s, adapter


@pytest.mark.asyncio
async def test_watchdog_triggers_on_duration_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_call_duration_seconds", 1)
    monkeypatch.setattr(settings, "caller_silence_timeout_seconds", 999)
    monkeypatch.setattr(settings, "caller_initial_silence_timeout_seconds", 999)

    s, adapter = _make_session()
    s._started_at = time.monotonic() - 5  # already past the cap
    s._last_caller_frame_at = time.monotonic()
    s._bot_has_spoken = True

    await asyncio.wait_for(s._pump_watchdog(), timeout=3.0)

    assert s._wrap_up_cause == "duration_cap"
    assert adapter.hangup_called is True
    assert s._stop.is_set()


@pytest.mark.asyncio
async def test_watchdog_triggers_on_caller_silence_after_bot_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_call_duration_seconds", 999)
    monkeypatch.setattr(settings, "caller_silence_timeout_seconds", 1)
    monkeypatch.setattr(settings, "caller_initial_silence_timeout_seconds", 999)

    s, adapter = _make_session()
    s._started_at = time.monotonic() - 30
    s._last_caller_frame_at = time.monotonic() - 5  # last frame 5s ago
    s._bot_has_spoken = True

    await asyncio.wait_for(s._pump_watchdog(), timeout=3.0)

    assert s._wrap_up_cause == "caller_silent"
    assert adapter.hangup_called is True


@pytest.mark.asyncio
async def test_watchdog_does_not_fire_caller_silence_before_bot_speaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-silence is only a problem after the bot has spoken at least once
    — before that we're in 'who talks first' territory and the initial-silence
    timeout governs instead.
    """
    monkeypatch.setattr(settings, "max_call_duration_seconds", 999)
    monkeypatch.setattr(settings, "caller_silence_timeout_seconds", 1)
    monkeypatch.setattr(settings, "caller_initial_silence_timeout_seconds", 999)

    s, adapter = _make_session()
    s._started_at = time.monotonic() - 30
    s._last_caller_frame_at = time.monotonic() - 30
    s._bot_has_spoken = False  # bot never spoke

    # The pump won't trigger caller_silent (bot hasn't spoken) and won't
    # trigger initial_silence (timeout is 999s). It should idle indefinitely;
    # we cancel after a short window to confirm no false trigger.
    task = asyncio.create_task(s._pump_watchdog())
    try:
        await asyncio.sleep(2.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert s._wrap_up_cause is None
    assert adapter.hangup_called is False


@pytest.mark.asyncio
async def test_watchdog_triggers_on_initial_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_call_duration_seconds", 999)
    monkeypatch.setattr(settings, "caller_silence_timeout_seconds", 999)
    monkeypatch.setattr(settings, "caller_initial_silence_timeout_seconds", 1)

    s, adapter = _make_session()
    s._started_at = time.monotonic() - 5
    s._last_caller_frame_at = time.monotonic() - 5
    s._bot_has_spoken = False

    await asyncio.wait_for(s._pump_watchdog(), timeout=3.0)

    assert s._wrap_up_cause == "initial_silence"
    assert adapter.hangup_called is True


@pytest.mark.asyncio
async def test_watchdog_exits_when_stop_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If something else ends the call, the watchdog must yield without firing."""
    monkeypatch.setattr(settings, "max_call_duration_seconds", 999)
    monkeypatch.setattr(settings, "caller_silence_timeout_seconds", 999)
    monkeypatch.setattr(settings, "caller_initial_silence_timeout_seconds", 999)

    s, adapter = _make_session()
    s._started_at = time.monotonic()
    s._last_caller_frame_at = time.monotonic()

    s._stop.set()
    await asyncio.wait_for(s._pump_watchdog(), timeout=2.0)

    assert s._wrap_up_cause is None
    assert adapter.hangup_called is False
