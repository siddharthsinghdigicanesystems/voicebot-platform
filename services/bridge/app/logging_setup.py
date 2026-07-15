"""Structured logging configuration.

Every log line is JSON, carries `service`, and (when set) the `call_id` and
`request_id` from contextvars. Aggregate with Loki/CloudWatch/Datadog without
extra parsing rules.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict

call_id_var: ContextVar[str | None] = ContextVar("call_id", default=None)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def _add_context(_: Any, __: str, event_dict: EventDict) -> EventDict:
    cid = call_id_var.get()
    rid = request_id_var.get()
    if cid:
        event_dict["call_id"] = cid
    if rid:
        event_dict["request_id"] = rid
    event_dict["service"] = "bridge"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level.upper(),
    )
    # silence the noisy ones; we still get errors
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()
