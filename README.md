# VoiceBot Platform

Production-grade AI voice bot platform that bridges **Tata SmartFlo Voice Streaming** with **OpenAI's GPT-4o Realtime API** to handle inbound and outbound phone calls with natural, sub-second-latency conversations.

## What this is

A real, runnable, production-patterned implementation of an AI voice agent that:

- Answers inbound calls and places outbound calls via Tata SmartFlo
- Streams audio bidirectionally with **end-to-end g711 μ-law @ 8 kHz** (no resampling — minimum latency)
- Uses **GPT-4o Realtime** for native speech-to-speech (STT + LLM + TTS in one model)
- Supports **interruptions / barge-in** out of the box (Realtime API handles VAD)
- Calls **CRM tools** during the conversation via OpenAI function-calling (`lookup_customer`, `schedule_appointment`, `transfer_to_human`, `end_call`)
- Stores transcripts, recordings (optional), and metrics in Postgres
- Exposes a React dashboard for call review, campaign management, and live monitoring
- Ships with a **mock telephony adapter + browser-mic client** so you can demo on a laptop with no Tata account

## Why these choices

| Decision | Rationale |
|---|---|
| **OpenAI Realtime** (not separate STT→LLM→TTS) | One WebSocket, one model. Cuts ~600 ms vs. a chained pipeline. Native barge-in. |
| **g711 μ-law end-to-end** | Tata streams μ-law @ 8 kHz; OpenAI Realtime accepts μ-law @ 8 kHz. Zero resampling = ~100 ms latency saved. |
| **WebSocket bridge as its own service** | Telephony loops are CPU-light but I/O-heavy and have very different scaling characteristics from the REST API. Separate process, separate scaling. |
| **Postgres + Redis** | Postgres is the system of record; Redis is for ephemeral session state and the campaign worker queue. |
| **Provider-agnostic adapter pattern** | `TelephonyAdapter` interface so Tata is one impl; mock adapter for dev; Twilio/Plivo trivial to add later. |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design and [`docs/tata-integration.md`](./docs/tata-integration.md) for production cutover.

## Repo layout

```
voicebot-platform/
├── services/
│   ├── bridge/           # Tata <-> OpenAI Realtime WebSocket bridge (Python)
│   ├── api/              # REST API: auth, calls, campaigns, CRM, webhooks (FastAPI)
│   ├── worker/           # Outbound campaign worker (RQ on Redis)
│   └── mock_telephony/   # Local Tata simulator + browser-mic demo client
├── frontend/             # React + TypeScript + Vite dashboard
├── docs/                 # ADRs, integration guides
├── docker-compose.yml    # One command to run everything
├── .env.example          # All required env vars, documented
└── Makefile              # Common dev tasks
```

## Quick start (local demo, no Tata account needed)

Prereqs: Docker Desktop, an `OPENAI_API_KEY` with Realtime API access.

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY=sk-...
docker compose up --build
```

Then open:

- **http://localhost:5173** — Dashboard (login: `admin` / `admin`)
- **http://localhost:8081** — Mock telephony page (click "Call the bot" — uses your mic)

To talk to the bot from the browser:

1. Open http://localhost:8081
2. Click **Start call**, allow mic access
3. Say *"Hi, I'd like to confirm my appointment"*
4. The bot will respond, look up your "customer record" via the mock CRM, and confirm
5. The transcript appears live in the dashboard

## Running with real Tata SmartFlo

See [`docs/tata-integration.md`](./docs/tata-integration.md). Summary:

1. Get Voice Streaming enabled on your Tata SmartFlo account
2. Set `TATA_*` vars in `.env`
3. Configure your Tata IVR to stream audio to `wss://YOUR-DOMAIN/v1/telephony/tata`
4. Configure Tata webhooks to `https://YOUR-DOMAIN/v1/webhooks/tata`
5. Deploy behind a TLS-terminating reverse proxy (Caddy / Nginx)

## Production patterns shipped

- **Structured JSON logging** (`structlog`) with correlation IDs across services
- **Prometheus metrics** at `/metrics` on every service
- **Health checks** at `/healthz` (liveness) and `/readyz` (readiness)
- **JWT auth** for the dashboard and inter-service calls
- **HMAC signature verification** for Tata webhooks
- **Alembic migrations** with autogenerate
- **Typed Python** (mypy strict on critical modules) + **typed React** (TS strict)
- **Tests** — unit (audio, agent, tools, recorder, watchdog), integration (bridge end-to-end with mock OpenAI), API (auth, calls, CRM, webhooks, campaigns + sweep + retry + bot config)
- **CI** — GitHub Actions: lint, type-check, test, docker-build
- **Graceful shutdown** with in-flight call drain
- **Rate limiting** on auth endpoints
- **Secrets via env** (never committed); `.env.example` documents every var
- **Bridge safety watchdog** — hard call-duration cap, post-speech silence timeout, and initial-silence timeout enforced server-side so a runaway model / muted caller / wedged WS can't burn unbounded minutes
- **Recordings to S3** with optional SSE-KMS, per-day prefix layout for lifecycle rules, and a clean disk fallback for dev / sidecar use
- **Outbound retry pipeline** — `retry_attempts` honored on busy/no_answer/failed; per-tick `sweep_stale` reverts crashed-mid-dial contacts to `pending` and abandons after retries are exhausted; `scheduled_at` gates claims so campaigns don't dial early
- **Per-campaign bot config** — `brand`, `voice`, `language` (en / hinglish / hi), and a full `system_prompt_override` field so different audiences get different prompts and voices without code changes

## Cost & latency expectations

For a typical 2-minute conversation with GPT-4o Realtime (as of late 2025):

| Item | Cost | Latency contribution |
|---|---|---|
| OpenAI Realtime audio in | ~$0.60 | — |
| OpenAI Realtime audio out | ~$0.50 | — |
| Tata trunk (India, outbound) | ~₹0.70 | — |
| **Total per call** | **~$1.10 + ₹0.70** | — |
| First-token latency (mic → bot speaks) | — | **~400–600 ms** |
| Tata leg | — | ~50 ms |
| OpenAI leg | — | ~300–500 ms |
| Bridge processing | — | <10 ms |

## License

MIT — see [`LICENSE`](./LICENSE).
