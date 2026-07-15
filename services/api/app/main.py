"""API service entrypoint."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import __version__
from app.config import settings
from app.logging_setup import configure_logging, get_logger, request_id_var
from app.routers import auth as auth_router
from app.routers import calls as calls_router
from app.routers import campaigns as campaigns_router
from app.routers import crm as crm_router
from app.routers import webhooks as webhooks_router

configure_logging(settings.log_level)
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "api_http_requests_total",
    "Total API requests by method, route, and status",
    ["method", "route", "status"],
)
http_request_duration_seconds = Histogram(
    "api_http_request_duration_seconds",
    "API request duration",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("api.startup", version=__version__, env=settings.env)
    yield
    log.info("api.shutdown")


app = FastAPI(title="VoiceBot API", version=__version__, lifespan=lifespan)

app.state.limiter = auth_router.limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(_: Request, __: RateLimitExceeded) -> JSONResponse:
    return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_public_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Middleware: request id + access metrics
# ---------------------------------------------------------------------------


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Response:
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = time.perf_counter() - start
        route = _route_template(request)
        http_requests_total.labels(request.method, route, "500").inc()
        http_request_duration_seconds.labels(request.method, route).observe(elapsed)
        raise
    elapsed = time.perf_counter() - start
    route = _route_template(request)
    http_requests_total.labels(request.method, route, str(response.status_code)).inc()
    http_request_duration_seconds.labels(request.method, route).observe(elapsed)
    response.headers["x-request-id"] = rid
    return response


def _route_template(request: Request) -> str:
    """Path with {param} placeholders so cardinality stays low for metrics."""
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    return request.url.path


# ---------------------------------------------------------------------------
# Health & metrics
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "version": __version__})


@app.get("/readyz")
async def readyz() -> JSONResponse:
    return JSONResponse({"ready": True})


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    body = generate_latest()
    return Response(body, media_type=CONTENT_TYPE_LATEST)


@app.get("/", response_class=PlainTextResponse)
async def root() -> str:
    return f"VoiceBot API {__version__} — see /docs"


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth_router.router)
app.include_router(crm_router.router)
app.include_router(calls_router.router)
app.include_router(campaigns_router.router)
app.include_router(webhooks_router.router)
