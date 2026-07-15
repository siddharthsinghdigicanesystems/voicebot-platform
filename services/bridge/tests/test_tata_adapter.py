"""Smartflo (Tata) Voice Streaming adapter tests.

These drive `TataAdapter` against a fake WebSocket that hands it a scripted
sequence of inbound frames and records what the adapter sends back. The focus
is strict-spec compliance: lifecycle, event routing, and — importantly — that
malformed / hostile client data is logged and skipped rather than crashing the
receive loop.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.telephony.base import CallDirection
from app.telephony.tata import TataAdapter

MULAW_FRAME = bytes(range(160))
MULAW_B64 = base64.b64encode(MULAW_FRAME).decode("ascii")


class FakeWebSocket:
    """Minimal stand-in for `starlette.websockets.WebSocket`.

    `inbound` is a list of already-serialized text frames (or the sentinel
    `WebSocketDisconnect` / `_CLOSE`) delivered in order to `receive_text()`.
    Everything the adapter sends is captured (parsed) in `sent`.
    """

    def __init__(self, inbound: list[Any]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict[str, Any]] = []
        self.client_state = WebSocketState.CONNECTED
        self.closed = False

    async def receive_text(self) -> str:
        if not self._inbound:
            raise WebSocketDisconnect(code=1000)
        item = self._inbound.pop(0)
        if item is WebSocketDisconnect:
            raise WebSocketDisconnect(code=1000)
        return item

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.client_state = WebSocketState.DISCONNECTED


def _frame(obj: dict[str, Any]) -> str:
    return json.dumps(obj)


def _start_frame(stream_sid: str = "STREAM123", **overrides: Any) -> str:
    start = {
        "accountSid": "ACC1",
        "streamSid": stream_sid,
        "callSid": "CALL1",
        "from": "+911111111111",
        "to": "+912222222222",
        "mediaFormat": {
            "encoding": "audio/x-mulaw",
            "sampleRate": 8000,
            "bitRate": 64,
            "bitDepth": 8,
        },
        "customParameters": {"FirstName": "Ada", "direction": "inbound"},
    }
    start.update(overrides)
    return _frame(
        {"event": "start", "sequenceNumber": "1", "start": start, "streamSid": stream_sid}
    )


def _media_frame(payload: str = MULAW_B64, seq: str = "3", stream_sid: str = "STREAM123") -> str:
    return _frame(
        {
            "event": "media",
            "sequenceNumber": seq,
            "media": {"chunk": "1", "timestamp": "5", "payload": payload},
            "streamSid": stream_sid,
        }
    )


async def _drain(adapter: TataAdapter) -> list[bytes]:
    return [frame async for frame in adapter.receive_audio()]


# --- lifecycle / start ------------------------------------------------------


@pytest.mark.asyncio
async def test_start_populates_session_state() -> None:
    ws = FakeWebSocket([_frame({"event": "connected"}), _start_frame()])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]

    ctx = await adapter.receive_call()

    assert adapter.stream_sid == "STREAM123"
    assert adapter.call_sid == "CALL1"
    assert adapter.account_sid == "ACC1"
    assert ctx.direction == CallDirection.INBOUND
    assert ctx.from_number == "+911111111111"
    assert ctx.to_number == "+912222222222"
    # Dynamic custom parameters are preserved verbatim.
    assert adapter.custom_parameters["FirstName"] == "Ada"
    assert ctx.extra["customParameters"]["FirstName"] == "Ada"


@pytest.mark.asyncio
async def test_start_tolerates_malformed_preamble() -> None:
    ws = FakeWebSocket(["{not json", _frame({"event": "connected"}), _start_frame()])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]

    ctx = await adapter.receive_call()

    assert ctx.provider_call_id == "CALL1"


@pytest.mark.asyncio
async def test_outbound_direction_from_custom_parameter() -> None:
    ws = FakeWebSocket(
        [_start_frame(customParameters={"direction": "outbound", "campaign_contact_id": "cc-9"})]
    )
    adapter = TataAdapter(ws)  # type: ignore[arg-type]

    ctx = await adapter.receive_call()

    assert ctx.direction == CallDirection.OUTBOUND
    assert ctx.extra["campaign_contact_id"] == "cc-9"


# --- media ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_frames_are_decoded_and_yielded() -> None:
    ws = FakeWebSocket([_media_frame(), _media_frame(seq="4"), _frame({"event": "stop"})])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME, MULAW_FRAME]


@pytest.mark.asyncio
async def test_stop_ends_receive_loop() -> None:
    stop = _frame(
        {"event": "stop", "stop": {"reason": "caller hung up"}, "streamSid": "STREAM123"}
    )
    ws = FakeWebSocket(
        [
            _media_frame(),
            stop,
            _media_frame(),  # must never be delivered — loop already returned
        ]
    )
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME]


@pytest.mark.asyncio
async def test_bad_base64_payload_is_skipped_not_fatal() -> None:
    ws = FakeWebSocket(
        [_media_frame(payload="!!!not-base64!!!"), _media_frame(seq="4"), _frame({"event": "stop"})]
    )
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME]  # only the good frame survives


@pytest.mark.asyncio
async def test_malformed_json_is_skipped_not_fatal() -> None:
    ws = FakeWebSocket(["}{ broken", _media_frame(), _frame({"event": "stop"})])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME]


@pytest.mark.asyncio
async def test_unknown_event_is_ignored() -> None:
    ws = FakeWebSocket(
        [
            _frame({"event": "wobble", "streamSid": "STREAM123"}),
            _media_frame(),
            _frame({"event": "stop"}),
        ]
    )
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME]


@pytest.mark.asyncio
async def test_media_missing_stream_sid_is_rejected() -> None:
    bad = _frame({"event": "media", "media": {"payload": MULAW_B64}})  # no streamSid
    ws = FakeWebSocket([bad, _media_frame(), _frame({"event": "stop"})])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME]


@pytest.mark.asyncio
async def test_media_mismatched_stream_sid_is_rejected() -> None:
    ws = FakeWebSocket(
        [_media_frame(stream_sid="OTHER"), _media_frame(), _frame({"event": "stop"})]
    )
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    frames = await _drain(adapter)

    assert frames == [MULAW_FRAME]


@pytest.mark.asyncio
async def test_media_rejected_when_encoding_not_mulaw() -> None:
    ws = FakeWebSocket([_media_frame(), _frame({"event": "stop"})])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"
    adapter._encoding_ok = False  # simulate a non-μ-law `start`

    frames = await _drain(adapter)

    assert frames == []


# --- dtmf -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dtmf_is_forwarded_to_hook() -> None:
    digits: list[str] = []
    dtmf = _frame(
        {"event": "dtmf", "sequenceNumber": "5", "dtmf": {"digit": "7"}, "streamSid": "STREAM123"}
    )
    ws = FakeWebSocket([dtmf, _frame({"event": "stop"})])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    async def on_dtmf(d: str) -> None:
        digits.append(d)

    adapter.on_dtmf = on_dtmf

    await _drain(adapter)

    assert digits == ["7"]


@pytest.mark.asyncio
async def test_invalid_dtmf_digit_is_not_forwarded() -> None:
    digits: list[str] = []
    ws = FakeWebSocket(
        [
            _frame({"event": "dtmf", "dtmf": {"digit": "Z"}, "streamSid": "STREAM123"}),
            _frame({"event": "stop"}),
        ]
    )
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    async def on_dtmf(d: str) -> None:
        digits.append(d)

    adapter.on_dtmf = on_dtmf

    await _drain(adapter)

    assert digits == []


# --- mark -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_ack_resolves_pending_mark_and_calls_hook() -> None:
    marks: list[str] = []
    mark = _frame(
        {
            "event": "mark",
            "sequenceNumber": "4",
            "mark": {"name": "bot-turn-1"},
            "streamSid": "STREAM123",
        }
    )
    ws = FakeWebSocket([mark, _frame({"event": "stop"})])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"
    adapter.pending_marks["bot-turn-1"] = 0.0

    async def on_mark(name: str) -> None:
        marks.append(name)

    adapter.on_mark = on_mark

    await _drain(adapter)

    assert marks == ["bot-turn-1"]
    assert "bot-turn-1" not in adapter.pending_marks


# --- outbound envelopes -----------------------------------------------------


@pytest.mark.asyncio
async def test_send_audio_envelope() -> None:
    ws = FakeWebSocket([])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    await adapter.send_audio(MULAW_FRAME)

    assert ws.sent == [
        {"event": "media", "streamSid": "STREAM123", "media": {"payload": MULAW_B64}}
    ]


@pytest.mark.asyncio
async def test_send_mark_tracks_pending() -> None:
    ws = FakeWebSocket([])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"

    await adapter.send_mark("bot-turn-1")

    assert ws.sent == [
        {"event": "mark", "streamSid": "STREAM123", "mark": {"name": "bot-turn-1"}}
    ]
    assert "bot-turn-1" in adapter.pending_marks


@pytest.mark.asyncio
async def test_clear_discards_pending_marks() -> None:
    ws = FakeWebSocket([])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    adapter.stream_sid = "STREAM123"
    adapter.pending_marks["bot-turn-1"] = 1.0

    await adapter.clear_buffer()

    assert ws.sent == [{"event": "clear", "streamSid": "STREAM123"}]
    assert adapter.pending_marks == {}


@pytest.mark.asyncio
async def test_send_audio_noop_before_start() -> None:
    ws = FakeWebSocket([])
    adapter = TataAdapter(ws)  # type: ignore[arg-type]
    # stream_sid is None until `start` arrives — sends must be dropped.
    await adapter.send_audio(MULAW_FRAME)
    await adapter.send_mark("x")
    await adapter.clear_buffer()

    assert ws.sent == []
