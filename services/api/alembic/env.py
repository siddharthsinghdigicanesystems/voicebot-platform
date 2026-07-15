"""Alembic environment.

We run migrations synchronously (alembic doesn't yet support async natively
without ceremony), but our app uses async SQLAlchemy. The DATABASE_URL is
translated from `postgresql+asyncpg://` to `postgresql+psycopg://` for
migrations, and back-translated for the app at runtime.

We rely on environment variables only — no ini-file URL.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db import Base  # noqa: F401  -- needed so models are imported
from app import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    """Return a sync-driver URL Alembic can use.

    The app uses async drivers at runtime (`+asyncpg`); Alembic itself runs
    sync, so we rewrite the driver portion. The replacements are scoped to
    the driver suffix to avoid the classic `+psycopg2` -> `+psycopg22`
    double-replace bug.
    """
    raw = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://voicebot:voicebot_dev_pw_change_me@postgres:5432/voicebot",
    )
    if "+asyncpg" in raw:
        return raw.replace("+asyncpg", "+psycopg2", 1)
    if "+psycopg2" in raw:
        return raw  # already sync; no rewrite needed
    if "+psycopg" in raw:
        return raw.replace("+psycopg", "+psycopg2", 1)
    return raw


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
