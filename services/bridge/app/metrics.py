"""Prometheus metrics exposed on a separate port.

Exposing metrics on a different port from the telephony WebSocket lets you
firewall the metrics endpoint from the public internet while keeping the
telephony WS endpoint reachable by Tata.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

calls_active = Gauge(
    "bridge_calls_active",
    "Number of currently active call bridges (Tata<->OpenAI pairs)",
)

calls_total = Counter(
    "bridge_calls_total",
    "Total calls handled, by direction and outcome",
    ["direction", "outcome"],
)

call_duration_seconds = Histogram(
    "bridge_call_duration_seconds",
    "Call duration from start event to hangup",
    buckets=(5, 10, 15, 30, 60, 90, 120, 180, 300, 600, 900),
)

first_response_latency_seconds = Histogram(
    "bridge_first_response_latency_seconds",
    "Latency from caller's first speech end to bot's first audio out",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0),
)

audio_frames_total = Counter(
    "bridge_audio_frames_total",
    "Audio frames forwarded, by direction (caller_to_bot or bot_to_caller)",
    ["direction"],
)

openai_errors_total = Counter(
    "bridge_openai_errors_total",
    "Errors from the OpenAI Realtime API, by error code",
    ["code"],
)

tool_invocations_total = Counter(
    "bridge_tool_invocations_total",
    "Tool calls executed during conversations, by tool and outcome",
    ["tool", "outcome"],
)


def metrics_app_response() -> tuple[bytes, str]:
    """Return (body, content_type) for a Prometheus scrape response."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
