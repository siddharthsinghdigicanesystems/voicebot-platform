"""Persistence helpers used by the session orchestrator.

Three kinds of persistence:

  1. Synchronous writes through the api service (call lifecycle, transcripts,
     tool invocations) — the source of truth lives in Postgres.
  2. Optional per-call audio recordings to disk or S3 (selected by
     `RECORDINGS_BACKEND`).
  3. Optional live transcript pub/sub via Redis so the dashboard can show
     a transcript as it's happening.

All three are best-effort: a persistence failure must NEVER drop the live call.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from abc import ABC, abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from app.api_client import ApiClient
from app.config import settings
from app.logging_setup import get_logger
from app.telephony.base import CallContext

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# API-backed persistence
# ---------------------------------------------------------------------------


async def persist_call_start(api: ApiClient, ctx: CallContext) -> dict[str, Any]:
    payload = {
        "provider_call_id": ctx.provider_call_id,
        "direction": ctx.direction.value,
        "from_number": ctx.from_number,
        "to_number": ctx.to_number,
        "audio_format": ctx.audio_format,
        "sample_rate": ctx.sample_rate,
        "metadata": ctx.extra,
    }
    return await api.call_started(payload)


async def persist_call_end(
    api: ApiClient,
    call_id: str,
    *,
    outcome: str,
    duration_s: float,
    facts: dict[str, Any],
) -> None:
    await api.call_ended(
        call_id,
        {"outcome": outcome, "duration_seconds": duration_s, "facts": facts},
    )


async def persist_transcript_segment(
    api: ApiClient,
    call_id: str,
    *,
    role: str,
    text: str,
    item_id: str | None,
) -> None:
    with suppress(Exception):
        await api.append_transcript(
            call_id,
            {"role": role, "text": text, "provider_item_id": item_id},
        )


async def persist_tool_invocation(
    api: ApiClient,
    call_id: str,
    *,
    name: str,
    arguments: dict[str, Any],
    result: Any,
) -> None:
    with suppress(Exception):
        await api.record_tool_invocation(
            call_id,
            {"name": name, "arguments": arguments, "result": result},
        )


# ---------------------------------------------------------------------------
# Recordings (disk; swap for S3 in prod with the same interface)
# ---------------------------------------------------------------------------


class Recorder(ABC):
    """Abstract per-call recorder.

    One inbound (caller) and one outbound (bot) μ-law track per call. We keep
    the tracks separate so post-call analysis can score them independently
    (e.g. caller sentiment vs. bot script adherence) and so a stereo merge
    job can downstream-encode them into a WAV.

    Backends ship for `disk` (dev / sidecar) and `s3` (prod). The factory
    `make_recorder()` picks based on `settings.recordings_backend`.
    """

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id

    @abstractmethod
    async def open(self) -> None:
        """Allocate any resources (open files, prepare buffers)."""

    @abstractmethod
    async def write_inbound(self, frame: bytes) -> None:
        """Append μ-law frame from the caller."""

    @abstractmethod
    async def write_outbound(self, frame: bytes) -> None:
        """Append μ-law frame from the bot."""

    @abstractmethod
    async def close(self) -> None:
        """Flush and release. Idempotent. Must not raise — recording is
        best-effort.
        """


class DiskRecorder(Recorder):
    """Writes both tracks straight to local disk.

    Use case: dev, sidecar containers with a mounted volume, or a simple
    NFS / persistent volume. Loses recordings on pod crash and offers no
    encryption — fine for non-production but not compliant for customer data.
    """

    def __init__(self, call_id: str, *, recordings_dir: str | None = None) -> None:
        super().__init__(call_id)
        self._dir = Path(recordings_dir or settings.recordings_dir)
        self._inbound_path = self._dir / f"{call_id}.inbound.ulaw"
        self._outbound_path = self._dir / f"{call_id}.outbound.ulaw"
        self._in_file: Any = None
        self._out_file: Any = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        self._in_file = await loop.run_in_executor(None, self._inbound_path.open, "wb")
        self._out_file = await loop.run_in_executor(None, self._outbound_path.open, "wb")

    async def write_inbound(self, frame: bytes) -> None:
        if self._in_file is None:
            return
        async with self._lock:
            self._in_file.write(frame)

    async def write_outbound(self, frame: bytes) -> None:
        if self._out_file is None:
            return
        async with self._lock:
            self._out_file.write(frame)

    async def close(self) -> None:
        for f in (self._in_file, self._out_file):
            if f is not None:
                with suppress(Exception):
                    f.close()
        self._in_file = None
        self._out_file = None
        log.info(
            "recording.closed",
            backend="disk",
            inbound=str(self._inbound_path),
            outbound=str(self._outbound_path),
        )


class S3Recorder(Recorder):
    """Buffers μ-law to a temp file, uploads to S3 on close.

    Why temp-file + upload-on-close (and not streaming multipart):

      - μ-law @ 8 kHz is ~480 KiB/min/track; a 10-minute call is ~10 MiB total.
        Well within local disk budget, and avoids the multipart bookkeeping.
      - Network blips during a call don't kill the recording — we only need
        S3 reachability at hangup.
      - Single PUT per track ⇒ atomic; no partially-uploaded objects to
        garbage-collect.

    Trade-off: a pod kill mid-call loses both tracks. Acceptable for a v1;
    for stronger durability move to multipart with per-minute parts.

    Object layout:
        s3://<bucket>/<prefix>/<YYYY/MM/DD>/<call_id>.inbound.ulaw
        s3://<bucket>/<prefix>/<YYYY/MM/DD>/<call_id>.outbound.ulaw

    Date prefix lets you set per-day S3 lifecycle rules (e.g. transition to
    Glacier after 30 days, delete after 365) without scanning every object.
    """

    def __init__(
        self,
        call_id: str,
        *,
        bucket: str | None = None,
        prefix: str | None = None,
        region: str | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        super().__init__(call_id)
        self.bucket = bucket or settings.recordings_s3_bucket
        self.prefix = (prefix or settings.recordings_s3_prefix).strip("/")
        self.region = region or settings.recordings_s3_region or None
        self.kms_key_id = kms_key_id or settings.recordings_s3_kms_key_id or None

        if not self.bucket:
            raise ValueError("S3Recorder requires recordings_s3_bucket")

        # Two temp files — one per track. We allocate during open(); close()
        # uploads then deletes them.
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._inbound_tmp: Path | None = None
        self._outbound_tmp: Path | None = None
        self._in_file: Any = None
        self._out_file: Any = None
        self._lock = asyncio.Lock()

    # The S3 key prefix for this call's date partition.
    @property
    def _key_prefix(self) -> str:
        from datetime import datetime

        d = datetime.utcnow().strftime("%Y/%m/%d")
        return f"{self.prefix}/{d}" if self.prefix else d

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._tmp_dir = tempfile.TemporaryDirectory(prefix=f"voicebot-{self.call_id}-")
        base = Path(self._tmp_dir.name)
        self._inbound_tmp = base / "inbound.ulaw"
        self._outbound_tmp = base / "outbound.ulaw"
        self._in_file = await loop.run_in_executor(None, self._inbound_tmp.open, "wb")
        self._out_file = await loop.run_in_executor(None, self._outbound_tmp.open, "wb")

    async def write_inbound(self, frame: bytes) -> None:
        if self._in_file is None:
            return
        async with self._lock:
            self._in_file.write(frame)

    async def write_outbound(self, frame: bytes) -> None:
        if self._out_file is None:
            return
        async with self._lock:
            self._out_file.write(frame)

    async def close(self) -> None:
        # Flush and close the temp files first so the bytes are on disk
        # before we hand them to boto3.
        for f in (self._in_file, self._out_file):
            if f is not None:
                with suppress(Exception):
                    f.close()
        self._in_file = None
        self._out_file = None

        if self._tmp_dir is None:
            return

        loop = asyncio.get_running_loop()
        keys: dict[str, str] = {}
        for label, path in (
            ("inbound", self._inbound_tmp),
            ("outbound", self._outbound_tmp),
        ):
            if path is None or not path.exists() or path.stat().st_size == 0:
                continue
            key = f"{self._key_prefix}/{self.call_id}.{label}.ulaw"
            try:
                await loop.run_in_executor(None, self._upload, path, key)
                keys[label] = key
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "recording.s3_upload_failed",
                    label=label,
                    bucket=self.bucket,
                    key=key,
                    error=str(exc),
                )

        # Whether upload succeeded or not, clean up the temp dir.
        with suppress(Exception):
            self._tmp_dir.cleanup()
        self._tmp_dir = None

        log.info(
            "recording.closed",
            backend="s3",
            bucket=self.bucket,
            keys=keys,
        )

    def _upload(self, path: Path, key: str) -> None:
        # boto3 is synchronous; we run it in the default thread executor.
        # Imported lazily so the disk path doesn't pay the import cost.
        import boto3  # type: ignore[import-not-found]

        kwargs: dict[str, Any] = {}
        if self.region:
            kwargs["region_name"] = self.region
        s3 = boto3.client("s3", **kwargs)

        extra: dict[str, Any] = {"ContentType": "audio/basic"}
        if self.kms_key_id:
            extra["ServerSideEncryption"] = "aws:kms"
            extra["SSEKMSKeyId"] = self.kms_key_id

        with open(path, "rb") as f:
            s3.upload_fileobj(f, self.bucket, key, ExtraArgs=extra)


def make_recorder(call_id: str) -> Recorder:
    """Factory: pick a backend per `settings.recordings_backend`.

    Catches misconfiguration (e.g. `s3` selected but bucket empty) and
    falls back to disk with a loud warning, so a config typo doesn't drop
    every recording in production.
    """
    backend = settings.recordings_backend
    if backend == "s3":
        try:
            return S3Recorder(call_id)
        except ValueError as exc:
            log.error("recording.s3_misconfigured_falling_back_to_disk", error=str(exc))
    return DiskRecorder(call_id)


# Optional helper: an env var to point S3 at a non-AWS endpoint (LocalStack /
# MinIO) for tests. boto3 reads `AWS_ENDPOINT_URL_S3` natively in modern
# versions; we leave it to the env so we don't surface a knob users won't need.
_ = os.environ.get("AWS_ENDPOINT_URL_S3")


# ---------------------------------------------------------------------------
# Live transcript pub/sub (Redis)
# ---------------------------------------------------------------------------


class LiveTranscriptPublisher:
    """Pushes each transcript segment onto a Redis pub/sub channel.

    The api service subscribes and re-emits to dashboard WebSockets.
    Channel: `transcript:<call_id>`
    """

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.channel = f"transcript:{call_id}"
        self._client: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )

    async def publish(self, *, role: str, text: str) -> None:
        if self._client is None:
            return
        with suppress(Exception):
            import json

            await self._client.publish(
                self.channel, json.dumps({"role": role, "text": text})
            )

    async def close(self) -> None:
        if self._client is not None:
            with suppress(Exception):
                await self._client.aclose()
            self._client = None
