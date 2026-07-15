"""Telephony adapter interface.

The bridge only talks to this interface. Adding a new provider = implementing
this. The interface intentionally stays small: bidirectional audio, lifecycle,
and the two side-channel operations we actually need (clear-buffer for barge-in
and transfer for human handoff).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class TelephonyEvent(str, Enum):
    """Out-of-band events the adapter may surface during a call."""

    HANGUP = "hangup"
    DTMF = "dtmf"
    ERROR = "error"


@dataclass
class CallContext:
    """Metadata describing a single call.

    `provider_call_id` is the carrier's identifier (e.g. Tata's `callSid`);
    we use it as the canonical key in the API service so logs from the carrier
    and our records line up.
    """

    provider_call_id: str
    direction: CallDirection
    from_number: str
    to_number: str
    audio_format: str = "g711_ulaw"
    sample_rate: int = 8000
    extra: dict[str, Any] = field(default_factory=dict)


class TelephonyAdapter(ABC):
    """Bidirectional audio bridge to a single live call.

    Out-of-band caller events (keypad presses and playback acknowledgements)
    are surfaced through the optional `on_dtmf` / `on_mark` hooks rather than
    the audio stream. The session orchestrator assigns them after
    `receive_call()`; the adapter invokes them from its receive loop. Keeping
    them as injected callbacks (not constructor args) is deliberate: the
    transport stays decoupled from business logic, so an adapter can be
    exercised in tests with no session attached.
    """

    #: Invoked with the pressed keypad digit ("0"-"9", "*", "#", "A"-"D").
    on_dtmf: Callable[[str], Awaitable[None]] | None = None
    #: Invoked with a mark label once the carrier reports it finished playing
    #: the previously-sent bot audio tagged with that label.
    on_mark: Callable[[str], Awaitable[None]] | None = None

    @abstractmethod
    async def receive_call(self) -> CallContext:
        """Block until the carrier delivers the call's start metadata."""

    @abstractmethod
    def receive_audio(self) -> AsyncIterator[bytes]:
        """Yield μ-law audio frames from the caller until the call ends."""

    @abstractmethod
    async def send_audio(self, mulaw_frame: bytes) -> None:
        """Send one or more μ-law frames toward the caller."""

    @abstractmethod
    async def send_mark(self, mark: str) -> None:
        """Send a named playback mark; the carrier echoes it when audio is played.

        We use this to know when our outbound audio actually reached the
        caller's ear (so first-response-latency measurements are meaningful).
        """

    @abstractmethod
    async def clear_buffer(self) -> None:
        """Drop any audio we've already queued at the carrier (for barge-in)."""

    @abstractmethod
    async def transfer(self, destination: str) -> None:
        """Hand off the call to a human agent / external number."""

    @abstractmethod
    async def hangup(self) -> None:
        """End the call gracefully."""

    @abstractmethod
    async def close(self) -> None:
        """Release adapter resources. Idempotent."""
