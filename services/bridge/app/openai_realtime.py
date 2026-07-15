"""Async client for the OpenAI Realtime API.

Why this is a thin client (and not the official SDK):

  - We need fine control over the WS lifecycle: explicit cancel on barge-in,
    custom audio framing, our own metrics, and the ability to interleave audio
    with tool results without buffering events.
  - The OpenAI Python SDK's realtime helpers are great for prototypes but
    abstract away the exact event ordering we depend on.

Reference: https://platform.openai.com/docs/api-reference/realtime
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect as ws_connect

from app.config import settings
from app.logging_setup import get_logger

log = get_logger(__name__)


class OpenAIRealtimeClient:
    """One client = one WebSocket = one Realtime session."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        url: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_realtime_model
        self.base_url = url or settings.openai_realtime_url
        self._ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()

    # --- connection -----------------------------------------------------------

    async def connect(self) -> None:
        url = f"{self.base_url}?model={self.model}"
        headers = [
            ("Authorization", f"Bearer {self.api_key}"),
            ("OpenAI-Beta", "realtime=v1"),
        ]
        log.info("openai.connect", model=self.model)
        # Explicitly use the new asyncio client (websockets>=13). The top-level
        # `websockets.connect` in 13.x still resolves to the legacy implementation,
        # which doesn't accept `additional_headers`.
        self._ws = await ws_connect(
            url,
            additional_headers=headers,
            max_size=2**24,    # 16 MiB; large enough for any single event
            ping_interval=20,
            ping_timeout=20,
            close_timeout=2,
        )

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("openai.close_error", error=str(exc))
            self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # --- session config -------------------------------------------------------

    async def configure_session(
        self,
        *,
        instructions: str,
        voice: str,
        tools: list[dict[str, Any]],
        input_audio_format: str = "g711_ulaw",
        output_audio_format: str = "g711_ulaw",
        temperature: float = 0.7,
        modalities: list[str] | None = None,
        turn_detection: dict[str, Any] | None = None,
    ) -> None:
        """Send the initial `session.update` event.

        We use server-side VAD by default — OpenAI does the speech-end detection
        and emits `response.create` automatically when the caller stops talking.
        """
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "modalities": modalities or ["text", "audio"],
                    "instructions": instructions,
                    "voice": voice,
                    "input_audio_format": input_audio_format,
                    "output_audio_format": output_audio_format,
                    "input_audio_transcription": {"model": "whisper-1"},
                    "turn_detection": turn_detection
                    or {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                    },
                    "tools": tools,
                    "tool_choice": "auto" if tools else "none",
                    "temperature": temperature,
                },
            }
        )

    # --- audio in/out ---------------------------------------------------------

    async def send_audio(self, mulaw_frame: bytes) -> None:
        """Forward a μ-law frame from the caller to the model."""
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(mulaw_frame).decode("ascii"),
            }
        )

    async def cancel_response(self) -> None:
        """Stop the current model response (used on barge-in)."""
        await self._send({"type": "response.cancel"})

    async def trigger_initial_greeting(self, prompt_hint: str | None = None) -> None:
        """Ask the model to speak first (for outbound calls).

        We post a system-style hint and call `response.create`. The model will
        speak its greeting before the user has said anything.
        """
        if prompt_hint:
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt_hint}],
                    },
                }
            )
        await self._send({"type": "response.create"})

    async def send_user_text(self, text: str, *, create_response: bool = True) -> None:
        """Inject a text turn as if the caller had spoken it.

        Used to forward out-of-band signals into the conversation — notably
        DTMF keypad presses, which have no audio for the model to hear. The
        text becomes a user conversation item; `create_response` then asks the
        model to react.
        """
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if create_response:
            await self._send({"type": "response.create"})

    async def submit_tool_result(self, call_id: str, output: Any) -> None:
        """Return a tool's result and let the model continue speaking."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output) if not isinstance(output, str) else output,
                },
            }
        )
        await self._send({"type": "response.create"})

    # --- event stream ---------------------------------------------------------

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield every server-sent event as a dict.

        The session orchestrator dispatches on `event["type"]`.
        """
        if self._ws is None:
            raise RuntimeError("not connected")
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                yield json.loads(raw)
        except websockets.ConnectionClosed as exc:
            log.info("openai.ws_closed", code=exc.code, reason=exc.reason)

    # --- internals ------------------------------------------------------------

    async def _send(self, obj: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("not connected")
        # Serialize sends to avoid interleaved frames on a shared WS.
        async with self._send_lock:
            await self._ws.send(json.dumps(obj, separators=(",", ":")))
