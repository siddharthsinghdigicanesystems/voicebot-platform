"""Wrap-up tests.

When the model calls `transfer_to_human` or `end_call`, the bot pump
must keep streaming audio to the caller until the model's post-tool
response actually finishes. The historical bug: it broke out of the
loop the instant the flag was set, then slept 1s in `_wrap_up` without
pumping audio — so the caller never heard the goodbye / "I'll connect
you" line and the call seemed to drop mid-sentence.

We exercise `Session._pump_bot_to_caller` against a fake OpenAI client
that yields a scripted event sequence, and assert:

  - Wrap-up happens AFTER the post-tool `response.done` (not before).
  - The audio frames sent during the goodbye reach `telephony.send_audio`.
  - If `response.done` never arrives, the grace cap forces a wrap-up.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.config import settings
from app.session import Session
from app.telephony.base import CallContext, CallDirection, TelephonyAdapter
from app.tools import ToolContext


class _FakeAdapter(TelephonyAdapter):
    def __init__(self) -> None:
        self.audio_frames_sent: list[bytes] = []
        self.hangup_called = False
        self.transfer_destination: str | None = None
        self.cleared = False

    async def receive_call(self) -> CallContext:  # pragma: no cover - unused here
        raise NotImplementedError

    def receive_audio(self) -> AsyncIterator[bytes]:  # pragma: no cover - unused here
        async def _empty() -> AsyncIterator[bytes]:
            if False:
                yield b""

        return _empty()

    async def send_audio(self, mulaw_frame: bytes) -> None:
        self.audio_frames_sent.append(mulaw_frame)

    async def send_mark(self, mark: str) -> None:
        return

    async def clear_buffer(self) -> None:
        self.cleared = True

    async def transfer(self, destination: str) -> None:
        self.transfer_destination = destination

    async def hangup(self) -> None:
        self.hangup_called = True

    async def close(self) -> None:
        return


class _ScriptedOpenAI:
    """Replays a hand-rolled list of Realtime events in order."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script
        self.cancel_calls = 0
        self.tool_results: list[tuple[str, Any]] = []

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        for event in self._script:
            yield event
            await asyncio.sleep(0)

    async def cancel_response(self) -> None:
        self.cancel_calls += 1

    async def submit_tool_result(self, call_id: str, output: Any) -> None:
        self.tool_results.append((call_id, output))


def _make_session() -> tuple[Session, _FakeAdapter, _ScriptedOpenAI]:
    adapter = _FakeAdapter()
    s = Session(adapter)
    # the real run() opens an OpenAI WS and an ApiClient; the wrap-up flow
    # under test only needs the pump and the tool ctx.
    s.tool_ctx = ToolContext(
        call=CallContext(
            provider_call_id="CA-test",
            direction=CallDirection.INBOUND,
            from_number="+911234567890",
            to_number="+911140000000",
        ),
        api=None,  # type: ignore[arg-type]
    )
    s.call_ctx = s.tool_ctx.call
    return s, adapter, None  # type: ignore[return-value]


def _audio_delta_b64(payload: bytes) -> dict[str, Any]:
    return {
        "type": "response.audio.delta",
        "delta": base64.b64encode(payload).decode("ascii"),
    }


@pytest.mark.asyncio
async def test_wrap_up_waits_for_post_tool_response_done() -> None:
    """The bot pump must keep forwarding goodbye audio until response.done.

    Sequence we replay:
      1. The model decides to transfer (we set the flag externally to simulate
         the tool dispatch having already happened).
      2. The OLD response (which contained the function call) completes.
      3. A NEW response.created arrives — this is the model speaking the
         "I'll connect you" goodbye in response to submit_tool_result.
      4. Audio deltas for the goodbye stream in.
      5. response.done arrives → only NOW should _wrap_up run.
    """
    s, adapter, _ = _make_session()

    # Simulate dispatch having already set the flag (the real flag is set
    # inside _handle_function_call_done — we shortcut that here).
    assert s.tool_ctx is not None
    s.tool_ctx.transfer_destination = "default_queue"

    goodbye_audio_1 = b"\xff" * 160  # μ-law silence-ish, 20ms frame
    goodbye_audio_2 = b"\x7f" * 160

    script = [
        {"type": "response.output_item.done"},  # closing function-call item
        {"type": "response.done"},  # OLD response (function-call) closes
        {"type": "response.created"},  # NEW response from submit_tool_result
        _audio_delta_b64(goodbye_audio_1),
        _audio_delta_b64(goodbye_audio_2),
        {"type": "response.audio.done"},
        {"type": "response.done"},  # NEW response done → wrap up
    ]
    s.openai = _ScriptedOpenAI(script)  # type: ignore[assignment]

    await asyncio.wait_for(s._pump_bot_to_caller(), timeout=3.0)

    # The goodbye audio must have reached the caller.
    assert adapter.audio_frames_sent == [goodbye_audio_1, goodbye_audio_2]
    # And the wrap-up must have actually run the transfer.
    assert adapter.transfer_destination == "default_queue"
    assert s._stop.is_set()


@pytest.mark.asyncio
async def test_wrap_up_does_not_fire_on_old_response_done() -> None:
    """The OLD response.done (the one carrying the function call) must not
    trigger wrap-up. We only wrap up after seeing a new response.created
    followed by its response.done."""
    s, adapter, _ = _make_session()
    assert s.tool_ctx is not None
    s.tool_ctx.end_call = True
    s.tool_ctx.end_reason = "completed"

    audio_frame = b"\xfe" * 160

    script = [
        # The OLD response.done arrives FIRST; the pump must not wrap up yet.
        {"type": "response.done"},
        # Then the NEW response from submit_tool_result starts and finishes.
        {"type": "response.created"},
        _audio_delta_b64(audio_frame),
        {"type": "response.done"},
    ]
    s.openai = _ScriptedOpenAI(script)  # type: ignore[assignment]

    await asyncio.wait_for(s._pump_bot_to_caller(), timeout=3.0)

    # Goodbye audio reached the caller (proves we kept pumping past the
    # old response.done instead of wrapping up immediately).
    assert adapter.audio_frames_sent == [audio_frame]
    assert adapter.hangup_called is True
    assert s._stop.is_set()


@pytest.mark.asyncio
async def test_wrap_up_grace_cap_forces_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If response.done never arrives (model hung, network glitch), the
    grace cap eventually forces a wrap-up so the call doesn't hang."""
    monkeypatch.setattr(settings, "wrap_up_grace_seconds", 0.2)

    s, adapter, _ = _make_session()
    assert s.tool_ctx is not None
    s.tool_ctx.end_call = True
    s.tool_ctx.end_reason = "completed"

    async def _stalled_events() -> AsyncIterator[dict[str, Any]]:
        # First event triggers the pending-wrap-up timer; then we just sit
        # here until the grace cap fires.
        yield {"type": "response.created"}
        await asyncio.sleep(2.0)
        yield {"type": "response.done"}  # never reached in time

    class _Stalled:
        cancel_calls = 0
        tool_results: list[Any] = []

        async def events(self) -> AsyncIterator[dict[str, Any]]:
            async for e in _stalled_events():
                yield e

        async def cancel_response(self) -> None:
            return

        async def submit_tool_result(self, call_id: str, output: Any) -> None:
            return

    s.openai = _Stalled()  # type: ignore[assignment]

    await asyncio.wait_for(s._pump_bot_to_caller(), timeout=2.0)

    # Wrap-up still happened (via the grace cap) so the call gets torn down.
    assert adapter.hangup_called is True
    assert s._stop.is_set()
