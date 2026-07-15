# ADR 0002 — Telephony bridge is its own service

**Date:** 2026-05-07
**Status:** Accepted

## Context

The naive design puts everything in one FastAPI app: REST endpoints, the dashboard WebSocket, and the carrier WebSocket all in one process.

## Decision

The telephony bridge runs as its own service (`services/bridge/`), separately deployed and scaled.

## Consequences

**Why this is right:**

- **Different connection profiles**: the api handles thousands of short HTTP requests; the bridge holds dozens of long-lived WebSocket pairs. They scale on different metrics and have different memory shapes.
- **Different deploy cadences**: prompt changes, dashboard changes, and tool implementation changes all hit the api. Each redeploy of the api drops live calls if they share a process. Splitting means the bridge is deployed only when its code or model config actually changes.
- **Blast radius**: a tool bug that crashes the bridge process loses live calls but doesn't affect the dashboard. A dashboard bug doesn't drop calls.
- **Different security posture**: the bridge's WS port is exposed to the telephony provider only (via firewall rules). The api is exposed to the public internet (for the dashboard) but not the telephony port.

**Trade-offs:**

- Two services to deploy and monitor instead of one.
- The bridge calls the api over HTTP for tool execution (CRM lookups, etc.). This adds ~5–20 ms per tool call. We accept this — tools are not on the audio hot path; they happen during natural pauses.

## Implementation note

The bridge authenticates to the api with a long-lived `SERVICE_TOKEN`. The api's `require_user_or_service` dependency accepts either a user JWT or this service token. This keeps tool endpoints reusable from both audiences (dashboard for testing, bridge for production).
