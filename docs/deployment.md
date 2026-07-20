# Deployment

The Compose file is the runnable artifact for development. For production, treat each service as an independent unit and deploy with the orchestrator of your choice.

## Production checklist

### Secrets

- [ ] Generate a strong `JWT_SECRET` (≥ 64 bytes of entropy: `python -c "import secrets; print(secrets.token_urlsafe(64))"`)
- [ ] Generate a strong `SERVICE_TOKEN` (separate from JWT_SECRET)
- [ ] Set `ADMIN_PASSWORD` to something unique; rotate after first login
- [ ] Configure `TATA_*` from your account manager
- [ ] Use a secret manager (AWS Secrets Manager, GCP Secret Manager, Vault) — never bake secrets into images

### Infrastructure

- [ ] Postgres (RDS / Cloud SQL / managed) with daily backups
- [ ] Redis (ElastiCache / Memorystore) — single instance is fine to start; cluster only at 10k+ concurrent calls
- [ ] Reverse proxy with TLS (Caddy, Nginx, or cloud LB) terminating in front of `bridge` and `api` — a ready-to-use single-domain Caddyfile ships at [`deploy/caddy/Caddyfile`](../deploy/caddy/Caddyfile)
- [ ] **Disable proxy buffering on the bridge's WebSocket path** — Nginx default is to buffer, which adds 100s of ms of jitter on the audio path (the shipped Caddyfile sets `flush_interval -1` on the audio route)

### Reverse proxy (single domain, Caddy)

[`deploy/caddy/Caddyfile`](../deploy/caddy/Caddyfile) terminates TLS (auto Let's Encrypt) and routes one public domain to all three services by path:

| Path | Service | Notes |
|---|---|---|
| `/v1/telephony/tata` | `bridge:8080` | Tata WSS audio stream. `flush_interval -1` + 1h timeouts for the long-lived, unbuffered audio loop. |
| `/v1/*` | `api:8000` | Webhooks, REST, dashboard live-transcript WS. |
| `/*` | `frontend:80` | Dashboard SPA. |

Configure Tata with `wss://YOUR-DOMAIN/v1/telephony/tata` and webhooks at `https://YOUR-DOMAIN/v1/webhooks/tata`. Only the canonical telephony path is proxied to the bridge — the bridge's `/` and `/websocket` aliases are intentionally left unrouted so they can't collide with the SPA. Keep the bridge's `9090` metrics port off the proxy (scrape it on the private network only).

Run it as a Caddy service alongside the stack via the production override, which mounts the Caddyfile and persists Let's Encrypt certs in the `caddy-data` volume:

```bash
# .env: set DOMAIN and ACME_EMAIL first
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Caddy is the only container that should publish host ports (`80`/`443`). For a hardened deploy, drop the `ports:` blocks for `api`, `bridge`, and `frontend` from `docker-compose.yml` (or override them) so those services are reachable only on the internal compose network, and restrict `443` inbound on the telephony path to Tata's published egress CIDRs.

### Render (Blueprint — free testing stack)

The repo ships a [`render.yaml`](../render.yaml) Blueprint configured for **$0 testing** (API + bridge only — no frontend/dashboard). In the Render dashboard: **New + → Blueprint → select this repo**. Render reads `render.yaml` and creates:

| Resource | Type | Root dir | Plan | Notes |
|---|---|---|---|---|
| `voicebot-db` | Postgres | — | free | deleted after 30 days — export before then |
| `voicebot-kv` | Key Value (Redis) | — | free | internal-only |
| `voicebot-api` | web (docker) | `services/api` | free | migrations on boot; `/healthz` |
| `voicebot-bridge` | web (docker) | `services/bridge` | free | cold-starts after idle |

**Why no frontend / worker?** Frontend is omitted for telephony/API testing. Render background workers have **no free tier** (Starter ~$7/mo), so outbound campaign dialing is disabled on this free stack; inbound/demo via the bridge still works.

**Free-tier trade-offs** (fine for testing, not live Tata traffic):

- Free web services **spin down after ~15 min idle**; the next request cold-starts in ~30–60s (breaks long-lived WSS until warm).
- Free Postgres is **hard-deleted after 30 days**.

How it wires together:

- **Secrets are prompted once.** Only `OPENAI_API_KEY`, `TATA_WEBHOOK_SECRET`, and `TATA_STREAMING_AUTH_TOKEN` are `sync: false`. Tata values can be placeholders (`test`) for now. `SERVICE_TOKEN`, `JWT_SECRET`, and `ADMIN_PASSWORD` are auto-generated — grab the admin password from the api Environment tab.
- **Managed Postgres/Redis are auto-linked** via `fromDatabase` / `fromService`. The api normalizes Render's `postgresql://` string to `+asyncpg` (see `services/api/app/config.py`).
- **`$PORT` binding**: api/bridge pin `PORT` to `8000`/`8080` so the private URL `http://voicebot-api:8000` is stable.
- `mock_telephony` and the dashboard frontend are excluded — local / later add-ons only.

**Upgrade path (when you can pay):** change `plan: free` → `plan: starter` on services that must stay warm (`bridge` first), and add a worker service:

```yaml
  - type: worker
    name: voicebot-worker
    runtime: docker
    rootDir: services/worker
    region: singapore
    plan: starter   # no free tier
    envVars:
      - fromGroup: voicebot-shared
      - key: API_INTERNAL_URL
        value: http://voicebot-api:8000
      - key: REDIS_URL
        fromService:
          type: keyvalue
          name: voicebot-kv
          property: connectionString
      - key: TATA_API_KEY
        sync: false
      - key: TATA_OUTBOUND_CALLER_ID
        sync: false
      - key: BRIDGE_PUBLIC_WS_URL
        sync: false
```

### Railway (monorepo)

This repo is a monorepo, so Railway must build **one service per subdirectory** — pointing a service at the repo root fails because the root has no `Dockerfile`/`requirements.txt` for Railpack to detect.

For each service, create a Railway service from this GitHub repo and set **Settings → Build → Root Directory**:

| Railway service | Root Directory | Public port |
|---|---|---|
| bridge | `services/bridge` | app binds `$PORT` (Tata WSS audio); `9090` metrics stays private |
| api | `services/api` | app binds `$PORT` |
| worker | `services/worker` | none (background process — leave unexposed) |
| frontend | `frontend` | nginx binds `$PORT` |

Each of those folders ships a `railway.json` that pins the Docker builder and restart policy, so once the root directory is set the build is reproducible. Notes:

- **Bind to `$PORT`, not a fixed port.** Railway injects `PORT` at runtime; the services read it (bridge/api via their start command, frontend via nginx's `envsubst` template) and fall back to their compose ports (`8080`/`8000`/`80`) locally.
- **Set env vars per service** in the Variables tab — same variables as the Compose `environment:` blocks (`JWT_SECRET`, `SERVICE_TOKEN`, `OPENAI_API_KEY`, `TATA_*`, `DATABASE_URL`, `REDIS_URL`, etc.). Use Railway's Postgres and Redis plugins and reference them with `${{Postgres.DATABASE_URL}}` / `${{Redis.REDIS_URL}}`.
- **Wire internal URLs** (`API_INTERNAL_URL`, `BRIDGE_PUBLIC_WS_URL`) to the services' Railway private-network hostnames.
- The `mock_telephony` service is a local browser-mic demo only — do not deploy it.

### Per-service notes

#### bridge

- Stateless across calls; horizontally scalable.
- Sticky sessions are NOT required. Each call opens its own WS pair.
- Resource budget: 1 CPU and 256 MiB sustains ~200 concurrent calls comfortably (CPU is dominated by base64 + JSON, both cheap).
- Scale on the `bridge_calls_active` metric. Useful Prometheus alert: `bridge_first_response_latency_seconds:p99 > 1.5` over 5 min.
- Set `terminationGracePeriodSeconds >= 90` so in-flight calls drain before a rolling deploy kills the pod.

#### api

- Stateless. Standard horizontal autoscaling on RPS / CPU.
- Read-replica Postgres for the dashboard's call list when traffic exceeds ~500 RPS.
- Rate limiting on `/v1/auth/login` ships at 5/min/IP via slowapi; tighten to 3/min/IP in production.

#### worker

- One worker pod is fine for hundreds of campaigns; scale to N pods for higher concurrency.
- The API uses `SELECT ... FOR UPDATE SKIP LOCKED` so multiple workers won't dial the same contact.

#### frontend

- The Vite build is hashed assets + an SPA shell — serve from any static host (S3+CloudFront, Cloud Storage+CDN, Nginx, Vercel, etc).
- Set `VITE_API_URL` at build time to your public api URL.
- The included Nginx config sets HSTS, X-Frame-Options, and Content-Security headers.

### Observability

- All services log JSON to stdout. Pipe to your log aggregator (Datadog, Loki, CloudWatch).
- Prometheus scrape targets:
  - `bridge:9090/metrics` (separate port from the WS endpoint — firewall the WS port to the carrier only)
  - `api:8000/metrics`
- Suggested SLOs:
  - Bridge: 99.5% of calls have `first_response_latency_seconds` < 1s
  - Bridge: 99.9% of calls complete with `outcome != error`
  - API: p95 latency < 200 ms

### Backups & retention

- Daily Postgres snapshot (RDS automated backups or `pg_dump` cron).
- Recordings (when enabled): swap `services/bridge/app/persistence.py:Recorder` to write to S3 with lifecycle rules (30/90 days depending on regulator).
- Transcripts are PII; encrypt the column at rest and apply RBAC at the API.

### Hardening

- Add a WAF in front of the api (CloudFront + AWS WAF, Cloudflare, etc.) — focus rules on the `/v1/auth/login` endpoint.
- The bridge's WS endpoint should accept connections only from Tata's egress IP range (Tata publishes a CIDR list; restrict at your firewall).
- Consider mTLS between the bridge and api (the JWT service-token is sufficient, but mTLS adds defense-in-depth).

## A note on costs

OpenAI Realtime is the dominant cost. As of late 2025:

- ~$0.06/minute audio in + ~$0.24/minute audio out (varies; check current pricing).
- A 2-minute call is ~$1.10.

For high-volume campaigns, consider:

- Setting an aggressive `max_concurrency` and `dial_timeout_seconds` to avoid burning audio-out tokens on no-answer dials.
- Using a cheaper model variant for lead qualification calls where conversation quality matters less.
- Falling back to a chained STT→LLM→TTS pipeline (Whisper + GPT-4o-mini + Sarvam TTS) for cost-sensitive volume — the `bridge` would need a second adapter for this; non-trivial but not architectural.
