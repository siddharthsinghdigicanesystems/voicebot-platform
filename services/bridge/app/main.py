"""Bridge service entrypoint.

  - WebSocket endpoint at /v1/telephony/tata receives audio streams from the
    carrier (Tata or local mock client). Each connection = one call = one Session.
  - HTTP endpoints at /healthz, /readyz for orchestrator probes.
  - Prometheus /metrics on a separate port (so it can be firewalled off the public WS port).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from app import __version__
from app.config import settings
from app.logging_setup import call_id_var, configure_logging, get_logger
from app.metrics import metrics_app_response
from app.session import Session
from app.telephony.mock import MockAdapter
from app.telephony.tata import TataAdapter

configure_logging(settings.log_level)
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Metrics HTTP server (separate port)
# ---------------------------------------------------------------------------


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            body, ctype = metrics_app_response()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_: object) -> None:  # silence default access log
        return


def _start_metrics_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", settings.metrics_port), _MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="metrics").start()
    log.info("metrics.listening", port=settings.metrics_port)
    return server


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    server = _start_metrics_server()
    log.info(
        "bridge.startup",
        version=__version__,
        env=settings.env,
        adapter=settings.telephony_adapter,
        model=settings.openai_realtime_model,
        voice=settings.openai_voice,
        audio_format=settings.audio_format,
    )
    try:
        yield
    finally:
        log.info("bridge.shutdown.draining_calls")
        # Best-effort drain: give in-flight calls a moment to wrap up. In real
        # k8s, terminationGracePeriodSeconds should be >= this + max call length
        # we want to preserve.
        await asyncio.sleep(2)
        server.shutdown()
        log.info("bridge.shutdown.done")


app = FastAPI(title="VoiceBot Bridge", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "version": __version__})


@app.get("/readyz")
async def readyz() -> JSONResponse:
    # If we've gotten here, FastAPI started; the metrics thread is up; we're ready.
    return JSONResponse({"ready": True})


@app.get("/")
async def root() -> PlainTextResponse:
    return PlainTextResponse(
        f"VoiceBot Bridge {__version__} — connect telephony at /v1/telephony/tata\n"
    )


# ---------------------------------------------------------------------------
# Telephony WebSocket endpoint
# ---------------------------------------------------------------------------


def _verify_streaming_auth(request_headers: dict[str, str], query_token: str | None) -> bool:
    """Verify the carrier's WS connection.

    Tata's Voice Streaming sends a Bearer token (header or `?token=` query). In
    dev with the mock adapter, no token is required. In production, set
    TATA_STREAMING_AUTH_TOKEN and the carrier will pass the matching value.
    """
    expected = settings.tata_streaming_auth_token
    if not expected:
        return True  # dev mode
    auth = request_headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth.split(" ", 1)[1] == expected:
        return True
    return query_token == expected


@app.websocket("/v1/telephony/tata")
async def telephony_ws(ws: WebSocket) -> None:
    headers = {k.decode().lower(): v.decode() for k, v in ws.scope.get("headers", [])}
    query_token = ws.query_params.get("token")

    if not _verify_streaming_auth(headers, query_token):
        log.warning("telephony.auth_failed", peer=ws.client.host if ws.client else None)
        await ws.close(code=4401)  # custom: unauthorized
        return

    await ws.accept()
    log.info(
        "telephony.connected",
        peer=ws.client.host if ws.client else None,
        adapter=settings.telephony_adapter,
    )

    adapter = (
        MockAdapter(ws) if settings.telephony_adapter == "mock" else TataAdapter(ws)
    )
    session = Session(adapter)
    try:
        await session.run()
    except WebSocketDisconnect:
        log.info("telephony.disconnect")
    except Exception as exc:  # noqa: BLE001
        log.exception("telephony.session_error", error=str(exc))
    finally:
        call_id_var.set(None)


# Convenience aliases for the same endpoint:
#   - "/"          — for a Tata config that drops the path prefix (some carrier
#                    UIs strip prefixes).
#   - "/websocket" — the route named in the Smartflo integration spec
#                    (wss://our-domain/websocket).
@app.websocket("/")
async def telephony_ws_root(ws: WebSocket) -> None:
    await telephony_ws(ws)


@app.websocket("/websocket")
async def telephony_ws_websocket(ws: WebSocket) -> None:
    await telephony_ws(ws)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _generic_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("http.unhandled", path=str(request.url), error=str(exc))
    return JSONResponse({"error": "internal_error"}, status_code=500)
