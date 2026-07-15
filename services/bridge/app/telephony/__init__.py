"""Telephony adapters.

Each provider (Tata, mock, future Twilio/Plivo) implements the
`TelephonyAdapter` interface. The session orchestrator only talks to that
interface, never to a provider directly.
"""

from __future__ import annotations

from .base import CallContext, CallDirection, TelephonyAdapter, TelephonyEvent

__all__ = ["CallContext", "CallDirection", "TelephonyAdapter", "TelephonyEvent"]
