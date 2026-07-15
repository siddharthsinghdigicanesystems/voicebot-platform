"""Runtime configuration loaded from environment.

All knobs live here. Anything that varies between dev/staging/prod is an env var
documented in `.env.example` at the repo root.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    # Core
    env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # OpenAI Realtime
    # Default pinned to the current GA snapshot (Aug 2025). Use `gpt-realtime`
    # for the rolling alias, `gpt-realtime-2` for the higher-reasoning model
    # (128k ctx), or `gpt-realtime-1.5` for the cheaper fast path.
    openai_api_key: str = Field(..., description="OpenAI API key with Realtime access")
    openai_realtime_model: str = "gpt-realtime-2025-08-28"
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    openai_voice: Literal["alloy", "echo", "shimmer", "verse", "ballad", "coral", "sage"] = "alloy"

    # Audio (kept end-to-end at the same format/rate as Tata to avoid resampling)
    audio_format: Literal["g711_ulaw", "g711_alaw", "pcm16"] = "g711_ulaw"
    audio_sample_rate: int = 8000

    # Telephony adapter selection
    telephony_adapter: Literal["mock", "tata"] = "mock"
    tata_streaming_auth_token: str = ""

    # Service-to-service
    api_internal_url: str = "http://api:8000"
    service_token: str = "dev-service-token"

    # Redis (used for live transcript pub/sub)
    redis_url: str = "redis://redis:6379/0"
    enable_live_transcript_pubsub: bool = True

    # Recordings
    enable_recordings: bool = False
    recordings_dir: str = "/app/recordings"
    recordings_backend: Literal["disk", "s3"] = "disk"
    recordings_s3_bucket: str = ""
    recordings_s3_prefix: str = "calls"
    recordings_s3_region: str = ""
    recordings_s3_kms_key_id: str = ""

    # Safety: hard upper bound on a call. The watchdog wraps things up
    # gracefully ~15 s before this and force-closes at the cap. This is the
    # last line of defense against a runaway loop billing minutes forever.
    max_call_duration_seconds: int = 600

    # Silence watchdog: end the call if the caller hasn't sent any audio for
    # this long after the bot started speaking (most commonly: caller hung up
    # without the carrier sending us a `stop`, or the WS link wedged).
    caller_silence_timeout_seconds: int = 30

    # If the caller never says anything at all (no media frames within this
    # window from call start), end the call. Independent from the post-speech
    # silence timeout above so we can tune the two independently.
    caller_initial_silence_timeout_seconds: int = 45

    # When the model calls `transfer_to_human` or `end_call`, we keep pumping
    # bot audio to the caller until that response's `response.done` arrives,
    # so the caller actually hears the goodbye / "I'll connect you" line.
    # This is the safety cap — if `response.done` doesn't fire within this
    # window, we give up and tear down anyway.
    wrap_up_grace_seconds: float = 8.0

    # Server
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080
    metrics_port: int = 9090


settings = Settings()  # type: ignore[call-arg]
