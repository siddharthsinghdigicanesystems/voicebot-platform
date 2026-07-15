"""Tata SmartFlo (Smartflo) Two-Way Voice Streaming adapter.

Smartflo initiates the WebSocket (WSS) connection to us; we are the server and
this adapter owns one connection = one call = one streaming session, keyed by
`streamSid`. The connection is full duplex: we continuously receive caller
audio (`media` events) and continuously send synthesized bot audio back.

Wire protocol (Smartflo -> us):

    {"event": "connected"}
    {"event": "start", "sequenceNumber": "1",
     "start": {"accountSid": "...", "streamSid": "...", "callSid": "...",
               "from": "...", "to": "...",
               "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000,
                               "bitRate": 64, "bitDepth": 8},
               "customParameters": {...}},
     "streamSid": "..."}
    {"event": "media", "sequenceNumber": "3",
     "media": {"chunk": "1", "timestamp": "5", "payload": "<base64 μ-law>"},
     "streamSid": "..."}
    {"event": "dtmf", "sequenceNumber": "5", "dtmf": {"digit": "1"},
     "streamSid": "..."}
    {"event": "mark", "sequenceNumber": "4", "mark": {"name": "label"},
     "streamSid": "..."}
    {"event": "stop", "stop": {"accountSid": "...", "callSid": "...",
                               "reason": "..."}, "streamSid": "..."}

Wire protocol (us -> Smartflo) — only these three are ever sent:

    {"event": "media", "streamSid": "...",
     "media": {"payload": "<base64 μ-law>", "chunk": 1}}
    {"event": "mark", "streamSid": "...", "mark": {"name": "unique_label"}}
    {"event": "clear", "streamSid": "..."}

Robustness contract (per integration spec): malformed JSON, unknown events, a
missing/mismatched `streamSid`, and undecodable audio must all be logged and
skipped — never crash the receive loop or the process. Raw audio payloads are
never logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.config import settings
from app.logging_setup import get_logger
from app.telephony.base import CallContext, CallDirection, TelephonyAdapter

log = get_logger(__name__)

# The only encoding Smartflo streams (and the only one we accept): G.711 μ-law.
EXPECTED_ENCODING = "audio/x-mulaw"

# Keypad digits we accept. Smartflo documents only 0-9 today, but the protocol
# allows the full DTMF alphabet; we forward all of them to business logic.
VALID_DTMF_DIGITS = frozenset("0123456789*#ABCD")


class TataAdapter(TelephonyAdapter):
    """One adapter instance handles one call (one WebSocket / one streamSid)."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        # --- session state (primary key + metadata from `start`) -------------
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.account_sid: str | None = None
        self.from_number: str = ""
        self.to_number: str = ""
        self.custom_parameters: dict[str, Any] = {}
        self.media_encoding: str = EXPECTED_ENCODING
        self._encoding_ok: bool = True
        # Labels we've sent in outbound `mark` events, awaiting the carrier's
        # matching `mark` ack. label -> monotonic time we sent it (for latency).
        self.pending_marks: dict[str, float] = {}
        self._last_sequence: str | None = None
        self._closed = False

    # --- lifecycle ------------------------------------------------------------

    async def receive_call(self) -> CallContext:
        """Wait for the `start` event and return the call context.

        Smartflo sends `connected` first, then `start`. We tolerate any
        preamble and never crash on a malformed frame while waiting.
        """
        while True:
            try:
                raw = await self.ws.receive_text()
            except WebSocketDisconnect:
                raise RuntimeError("Smartflo disconnected before `start`") from None

            evt = self._parse_event(raw)
            if evt is None:
                continue
            etype = evt.get("event")

            if etype == "connected":
                log.info("tata.connected")
                continue
            if etype == "start":
                return self._handle_start(evt)
            if etype == "stop":
                raise RuntimeError("Smartflo closed stream before `start` was received")
            # Any other event before `start` is out of lifecycle order: log,
            # ignore, keep waiting.
            log.info(
                "tata.event_before_start",
                evt_type=etype,
                sequence=evt.get("sequenceNumber"),
            )

    def _handle_start(self, evt: dict[str, Any]) -> CallContext:
        start = evt.get("start", {}) or {}
        # `streamSid` is the primary key for the whole session; it may live at
        # the top level, inside `start`, or both — accept either.
        self.stream_sid = evt.get("streamSid") or start.get("streamSid")
        if not self.stream_sid:
            raise RuntimeError("Smartflo `start` event is missing streamSid")

        self.call_sid = start.get("callSid", self.stream_sid)
        self.account_sid = start.get("accountSid")
        params = start.get("customParameters", {}) or {}
        self.custom_parameters = dict(params)  # dynamic; never hardcode keys
        self.from_number = params.get("from", start.get("from", "")) or ""
        self.to_number = params.get("to", start.get("to", "")) or ""
        self._last_sequence = evt.get("sequenceNumber")

        media_format = start.get("mediaFormat", {}) or {}
        self.media_encoding = media_format.get("encoding", EXPECTED_ENCODING)
        self._encoding_ok = self.media_encoding == EXPECTED_ENCODING
        if not self._encoding_ok:
            # We only speak μ-law. Log loudly; media packets will be rejected.
            log.error(
                "tata.unexpected_media_format",
                stream_sid=self.stream_sid,
                encoding=self.media_encoding,
                expected=EXPECTED_ENCODING,
            )

        direction = (
            CallDirection.OUTBOUND
            if params.get("direction") == "outbound"
            else CallDirection.INBOUND
        )
        # Promote `campaign_contact_id` to a first-class field on `extra` so the
        # bridge can fetch the campaign's bot config without re-parsing
        # customParameters everywhere. All custom params are preserved as-is.
        extra: dict[str, Any] = {
            "streamSid": self.stream_sid,
            "accountSid": self.account_sid,
            "customParameters": self.custom_parameters,
            "tata_start": start,
        }
        if params.get("campaign_contact_id"):
            extra["campaign_contact_id"] = params["campaign_contact_id"]

        ctx = CallContext(
            provider_call_id=self.call_sid or self.stream_sid,
            direction=direction,
            from_number=self.from_number,
            to_number=self.to_number,
            audio_format="g711_ulaw",
            sample_rate=8000,
            extra=extra,
        )
        log.info(
            "tata.start",
            evt_type="start",
            stream_sid=self.stream_sid,
            call_sid=self.call_sid,
            account_sid=self.account_sid,
            sequence=self._last_sequence,
            direction=direction.value,
            custom_parameter_keys=sorted(self.custom_parameters.keys()),
        )
        return ctx

    # --- audio in -------------------------------------------------------------

    async def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield raw μ-law frames from the caller.

        This is the single receive loop / event router for the whole call.
        Every non-`media` event (dtmf, mark, stop, unknown) is dispatched here
        too. A single bad frame is logged and skipped; only a websocket
        disconnect or `stop` ends the loop.
        """
        try:
            while not self._closed:
                try:
                    raw = await self.ws.receive_text()
                except WebSocketDisconnect:
                    log.info("tata.ws_disconnect", stream_sid=self.stream_sid)
                    return

                evt = self._parse_event(raw)
                if evt is None:
                    continue

                etype = evt.get("event")
                if etype == "media":
                    frame = self._decode_media(evt)
                    if frame is not None:
                        yield frame
                elif etype == "stop":
                    self._handle_stop(evt)
                    return
                elif etype == "dtmf":
                    await self._handle_dtmf(evt)
                elif etype == "mark":
                    await self._handle_mark(evt)
                elif etype == "connected":
                    # Duplicate/late handshake; nothing to do.
                    log.debug("tata.connected_again", stream_sid=self.stream_sid)
                else:
                    # Unknown event types MUST NOT crash us. Log and ignore.
                    log.info(
                        "tata.unknown_event",
                        evt_type=etype,
                        stream_sid=self.stream_sid,
                        sequence=evt.get("sequenceNumber"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let one bad frame kill the call
            log.warning("tata.recv_error", stream_sid=self.stream_sid, error=str(exc))

    def _decode_media(self, evt: dict[str, Any]) -> bytes | None:
        if not self._require_stream_sid(evt, "media"):
            return None
        if not self._encoding_ok:
            log.warning(
                "tata.reject_packet_bad_format",
                stream_sid=self.stream_sid,
                encoding=self.media_encoding,
            )
            return None
        media = evt.get("media", {}) or {}
        payload = media.get("payload", "")
        if not payload:
            return None
        try:
            frame = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            log.warning(
                "tata.reject_packet_bad_base64",
                stream_sid=self.stream_sid,
                sequence=evt.get("sequenceNumber"),
                error=str(exc),
            )
            return None
        self._last_sequence = evt.get("sequenceNumber", self._last_sequence)
        # Preserve chunk/timestamp/sequence in logs; NEVER log the payload.
        log.debug(
            "tata.media",
            evt_type="media",
            stream_sid=self.stream_sid,
            call_sid=self.call_sid,
            sequence=evt.get("sequenceNumber"),
            chunk=media.get("chunk"),
            timestamp=media.get("timestamp"),
            bytes=len(frame),
        )
        return frame

    def _handle_stop(self, evt: dict[str, Any]) -> None:
        stop = evt.get("stop", {}) or {}
        log.info(
            "tata.stop",
            evt_type="stop",
            stream_sid=self.stream_sid or evt.get("streamSid") or stop.get("streamSid"),
            call_sid=stop.get("callSid", self.call_sid),
            account_sid=stop.get("accountSid", self.account_sid),
            sequence=evt.get("sequenceNumber"),
            reason=stop.get("reason"),  # informational only
        )

    async def _handle_dtmf(self, evt: dict[str, Any]) -> None:
        if not self._require_stream_sid(evt, "dtmf"):
            return
        digit = str((evt.get("dtmf", {}) or {}).get("digit", ""))
        if digit not in VALID_DTMF_DIGITS:
            log.warning(
                "tata.dtmf_invalid",
                stream_sid=self.stream_sid,
                sequence=evt.get("sequenceNumber"),
                digit=digit,
            )
            return
        log.info(
            "tata.dtmf",
            evt_type="dtmf",
            stream_sid=self.stream_sid,
            call_sid=self.call_sid,
            sequence=evt.get("sequenceNumber"),
            digit=digit,
        )
        if self.on_dtmf is not None:
            try:
                await self.on_dtmf(digit)
            except Exception as exc:  # noqa: BLE001
                log.warning("tata.dtmf_handler_error", digit=digit, error=str(exc))

    async def _handle_mark(self, evt: dict[str, Any]) -> None:
        if not self._require_stream_sid(evt, "mark"):
            return
        name = (evt.get("mark", {}) or {}).get("name", "")
        sent_at = self.pending_marks.pop(name, None)
        playback_latency = None if sent_at is None else round(time.monotonic() - sent_at, 3)
        log.info(
            "tata.mark",
            evt_type="mark",
            stream_sid=self.stream_sid,
            call_sid=self.call_sid,
            sequence=evt.get("sequenceNumber"),
            mark=name,
            latency=playback_latency,
            outstanding_marks=len(self.pending_marks),
        )
        if self.on_mark is not None:
            try:
                await self.on_mark(name)
            except Exception as exc:  # noqa: BLE001
                log.warning("tata.mark_handler_error", mark=name, error=str(exc))

    # --- audio out ------------------------------------------------------------

    async def send_audio(self, mulaw_frame: bytes) -> None:
        if self._closed or self.stream_sid is None:
            return
        await self._send_json(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(mulaw_frame).decode("ascii")},
            }
        )

    async def send_mark(self, mark: str) -> None:
        if self._closed or self.stream_sid is None:
            return
        # Remember the label until Smartflo echoes it back in a `mark` event.
        self.pending_marks[mark] = time.monotonic()
        await self._send_json(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": mark}}
        )

    async def clear_buffer(self) -> None:
        if self._closed or self.stream_sid is None:
            return
        # `clear` discards Smartflo's buffered outgoing audio, so any marks we
        # were still waiting on for that audio will never (usefully) return.
        self.pending_marks.clear()
        await self._send_json({"event": "clear", "streamSid": self.stream_sid})

    # --- side channels: transfer + hangup --------------------------------------

    async def transfer(self, destination: str) -> None:
        """Transfer via Tata's REST API (the WS protocol can't transfer).

        Note: call transfer is NOT supported by Smartflo's alpha Voice
        Streaming platform. This remains as a REST side-channel for accounts
        where it is enabled; the bridge only calls it when the model invokes
        `transfer_to_human`.
        """
        if not self.call_sid:
            log.warning("tata.transfer.no_call_sid")
            return
        if not settings.tata_streaming_auth_token:
            log.warning("tata.transfer.no_token; skipping (dev mode)")
            return
        url = f"https://api-smartflo.tatateleservices.com/v1/calls/{self.call_sid}/transfer"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {settings.tata_streaming_auth_token}"},
                    json={"destination": destination},
                )
                r.raise_for_status()
            log.info("tata.transfer.ok", destination=destination)
        except Exception as exc:  # noqa: BLE001
            log.error("tata.transfer.failed", error=str(exc))

    async def hangup(self) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.ws.client_state != WebSocketState.DISCONNECTED:
                await self.ws.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("tata.close_error", error=str(exc))

    # --- internals ------------------------------------------------------------

    def _parse_event(self, raw: str) -> dict[str, Any] | None:
        """Parse one frame. Malformed JSON / non-object frames are logged and
        dropped (return None) so the receive loop keeps running."""
        try:
            evt = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("tata.malformed_json", stream_sid=self.stream_sid, error=str(exc))
            return None
        if not isinstance(evt, dict):
            log.warning("tata.non_object_event", stream_sid=self.stream_sid)
            return None
        return evt

    def _require_stream_sid(self, evt: dict[str, Any], event_name: str) -> bool:
        """Reject events with a missing or mismatched streamSid (spec: the
        streamSid must remain identical for the whole session)."""
        sid = evt.get("streamSid")
        if not sid:
            log.error(
                "tata.missing_stream_sid",
                evt_type=event_name,
                sequence=evt.get("sequenceNumber"),
            )
            return False
        if self.stream_sid is not None and sid != self.stream_sid:
            log.error(
                "tata.stream_sid_mismatch",
                evt_type=event_name,
                expected=self.stream_sid,
                got=sid,
            )
            return False
        return True

    async def _send_json(self, obj: dict[str, Any]) -> None:
        try:
            await self.ws.send_text(json.dumps(obj, separators=(",", ":")))
        except (WebSocketDisconnect, RuntimeError) as exc:
            self._closed = True
            log.info("tata.send_after_close", error=str(exc))
        except asyncio.CancelledError:
            raise
