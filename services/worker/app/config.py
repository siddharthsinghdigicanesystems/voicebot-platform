"""Worker settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    log_level: str = "INFO"

    api_internal_url: str = "http://api:8000"
    service_token: str = "dev-service-token"

    # Tata
    tata_api_key: str = ""
    tata_api_base_url: str = "https://api-smartflo.tatateleservices.com"
    tata_outbound_caller_id: str = ""
    bridge_public_ws_url: str = "ws://bridge:8080/v1/telephony/tata"

    # Pacing
    poll_interval_seconds: float = 2.0
    per_campaign_concurrency: int = 5  # also enforced at the API; this is a safety cap
    dial_timeout_seconds: float = 30.0

    # Stale-dialing sweep: how old a `dialing` row must be before the sweeper
    # reverts it (to `pending` if attempts left, else `failed`). Should be
    # comfortably greater than `dial_timeout_seconds` plus a real call's max
    # duration so we never recover an in-flight call. Defaults: dial_timeout
    # + bridge max_call_duration (10 min) + slack ≈ 12 min.
    sweep_stale_older_than_seconds: int = 720


settings = Settings()  # type: ignore[call-arg]
