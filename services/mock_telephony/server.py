"""Mock telephony static server.

Hosts the browser-mic demo page on :8081. The page itself opens a WebSocket
**directly** to the bridge — this server is purely for hosting static files
and runtime config.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Mock Telephony")

# Bridge URL the page should connect to. Inside docker-compose this is the
# service name; on the host, localhost. The browser is on the host, so we
# default to a localhost URL — the env var BRIDGE_PUBLIC_WS_URL overrides.
BRIDGE_WS_URL = os.environ.get("BRIDGE_PUBLIC_WS_URL", "ws://localhost:8080/v1/telephony/tata")


@app.get("/config.json")
async def config() -> JSONResponse:
    return JSONResponse({"bridge_ws_url": BRIDGE_WS_URL})


app.mount("/", StaticFiles(directory="public", html=True), name="public")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="warning")
