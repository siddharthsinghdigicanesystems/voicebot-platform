"""Shared pytest fixtures.

We set OPENAI_API_KEY to a dummy value at import time so importing
`app.config` never fails in CI. Real credentials are not required for
the unit tests in this directory.
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("API_INTERNAL_URL", "http://api.test")
