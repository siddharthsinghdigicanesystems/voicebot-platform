"""Recorder backend tests.

We exercise the disk path end-to-end (writes hit the filesystem) and the
S3 path with `boto3.client` patched so we don't need real credentials.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.persistence import DiskRecorder, S3Recorder, make_recorder


@pytest.mark.asyncio
async def test_disk_recorder_writes_both_tracks(tmp_path: Path) -> None:
    rec = DiskRecorder("CA-disk-1", recordings_dir=str(tmp_path))
    await rec.open()
    await rec.write_inbound(b"\x80" * 160)
    await rec.write_inbound(b"\x80" * 160)
    await rec.write_outbound(b"\xff" * 160)
    await rec.close()

    inbound = tmp_path / "CA-disk-1.inbound.ulaw"
    outbound = tmp_path / "CA-disk-1.outbound.ulaw"
    assert inbound.exists() and inbound.stat().st_size == 320
    assert outbound.exists() and outbound.stat().st_size == 160


@pytest.mark.asyncio
async def test_disk_recorder_close_is_idempotent(tmp_path: Path) -> None:
    rec = DiskRecorder("CA-disk-2", recordings_dir=str(tmp_path))
    await rec.open()
    await rec.close()
    # Second close should be a no-op, not raise.
    await rec.close()


def test_make_recorder_falls_back_to_disk_when_s3_misconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty bucket selector must NOT cause us to drop recordings — the
    factory returns a DiskRecorder so calls keep recording locally even if
    someone fat-fingers the deploy config.
    """
    monkeypatch.setattr(settings, "recordings_backend", "s3")
    monkeypatch.setattr(settings, "recordings_s3_bucket", "")

    rec = make_recorder("CA-fallback")
    assert isinstance(rec, DiskRecorder)


@pytest.mark.asyncio
async def test_s3_recorder_uploads_each_track_with_kms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive S3Recorder with a fake boto3 client and verify it would have
    uploaded both tracks with the configured KMS key.
    """
    monkeypatch.setattr(settings, "recordings_backend", "s3")
    monkeypatch.setattr(settings, "recordings_s3_bucket", "test-bucket")
    monkeypatch.setattr(settings, "recordings_s3_prefix", "calls")
    monkeypatch.setattr(settings, "recordings_s3_region", "ap-south-1")
    monkeypatch.setattr(settings, "recordings_s3_kms_key_id", "alias/voicebot")

    fake_client = MagicMock()
    with patch("boto3.client", return_value=fake_client) as boto3_client:
        rec = S3Recorder("CA-s3-1")
        await rec.open()
        await rec.write_inbound(b"\x80" * 160)
        await rec.write_outbound(b"\xff" * 160)
        await rec.close()

    boto3_client.assert_called_with("s3", region_name="ap-south-1")
    # Two tracks => two uploads
    assert fake_client.upload_fileobj.call_count == 2
    keys = [call.args[2] for call in fake_client.upload_fileobj.call_args_list]
    assert any(k.endswith("/CA-s3-1.inbound.ulaw") for k in keys)
    assert any(k.endswith("/CA-s3-1.outbound.ulaw") for k in keys)
    assert all(k.startswith("calls/") for k in keys)

    # Check ExtraArgs got SSE-KMS
    extras = [call.kwargs["ExtraArgs"] for call in fake_client.upload_fileobj.call_args_list]
    for e in extras:
        assert e["ServerSideEncryption"] == "aws:kms"
        assert e["SSEKMSKeyId"] == "alias/voicebot"
        assert e["ContentType"] == "audio/basic"


@pytest.mark.asyncio
async def test_s3_recorder_skips_empty_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    """If no audio was ever written for a track (e.g. the bot never spoke
    before the call ended), don't upload an empty object.
    """
    monkeypatch.setattr(settings, "recordings_s3_bucket", "test-bucket")

    fake_client = MagicMock()
    with patch("boto3.client", return_value=fake_client):
        rec = S3Recorder("CA-empty")
        await rec.open()
        # No writes at all.
        await rec.close()

    assert fake_client.upload_fileobj.call_count == 0


@pytest.mark.asyncio
async def test_s3_recorder_close_swallows_upload_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistence must NEVER drop the live call: an S3 upload failure is
    a log line, not an exception.
    """
    monkeypatch.setattr(settings, "recordings_s3_bucket", "test-bucket")

    fake_client = MagicMock()
    fake_client.upload_fileobj.side_effect = RuntimeError("S3 down")
    with patch("boto3.client", return_value=fake_client):
        rec = S3Recorder("CA-error")
        await rec.open()
        await rec.write_inbound(b"\x80" * 160)
        # Must not raise.
        await rec.close()
