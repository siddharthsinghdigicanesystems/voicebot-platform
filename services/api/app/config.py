"""API service settings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = (
        "postgresql+asyncpg://voicebot:voicebot_dev_pw_change_me@postgres:5432/voicebot"
    )
    database_ssl: Literal["disable", "require"] = "disable"

    # Redis (for live transcript pub/sub fan-out to dashboard WS)
    redis_url: str = "redis://redis:6379/0"

    # Auth
    jwt_secret: str = Field("dev-secret-replace-me", min_length=12)
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_seconds: int = 3600
    service_token: str = "dev-service-token"

    # Default seeded admin (created idempotently on startup)
    admin_username: str = "admin"
    admin_password: str = "admin"

    # CORS
    frontend_public_url: str = "http://localhost:5173"

    # Tata
    tata_webhook_secret: str = ""

    # Server
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000


settings = Settings()  # type: ignore[call-arg]
