# Architecture

## High-level

```
                 PSTN / mobile network
                          |
                  ┌───────▼────────┐
                  │ Tata SmartFlo  │  IVR + DID + outbound trunk
                  │  Voice Stream  │
                  └───────┬────────┘
                          │ WSS (μ-law @ 8kHz, bidirectional)
                          │
              ┌───────────▼────────────┐
              │   bridge service       │  Python / asyncio
              │   - WS server (Tata)   │
              │   - WS client (OpenAI) │  ◄────────┐
              │   - Session orchestrator          │
              │   - Tool dispatcher    │          │
              └─────┬──────────┬───────┘          │
                    │          │                  │ WSS
                    │ HTTP     │ persist          │
                    ▼          ▼                  │
       ┌──────────────────┐  ┌──────────┐  ┌─────▼───────────┐
       │   api service    │  │ Postgres │  │  OpenAI         │
       │   - REST + auth  │◄─┤          │  │  Realtime API   │
       │   - mock CRM     │  └──────────┘  │  (gpt-4o-       │
       │   - webhooks     │                │   realtime)     │
       └─────┬────────────┘                └─────────────────┘
             │                                    
             │ HTTP                          ┌──────────┐
             ▼                               │  Redis   │
       ┌──────────────────┐                  │ (queue + │
       │   frontend       │                  │  pubsub) │
       │   React + TS     │                  └─────┬────┘
       └──────────────────┘                        │
                                                   │ RQ
                                            ┌──────▼──────┐
                                            │   worker    │  outbound
                                            │  - Tata C2C │  campaigns
                                            │  - retries  │
                                            └─────────────┘
```

## Why a separate bridge service?

The telephony bridge has a fundamentally different shape from the REST API:

| Concern | API service | Bridge service |
|---|---|---|
| Connections per pod | thousands of short HTTP | dozens of long-lived WS pairs |
| CPU profile | bursty | steady (audio loop) |
| Memory profile | per-request | per-call (~few MB sustained) |
| Failure mode | a 500 fails one request | a crash drops live calls |
| Deploy cadence | frequent | careful, drain in-flight |

Conflating them means restarting the API drops every live call. So we split.

## Audio path (the hot loop)

```
Tata WS frame (μ-law, 20ms, 160 bytes)
   │
   ▼
[bridge: TelephonyAdapter.recv()]
   │
   ▼
[bridge: forward to OpenAI Realtime as input_audio_buffer.append]
                                                                  
[OpenAI: VAD detects speech end → generates response]             
                                                                  
[OpenAI Realtime → response.audio.delta (μ-law, base64)]          
   │                                                              
   ▼                                                              
[bridge: TelephonyAdapter.send(audio)]                            
   │
   ▼
Tata WS frame back to caller
```

The bridge does **no** audio processing — no resampling, no codec conversion, no buffering beyond what's needed for backpressure. Every ms saved here is a ms of perceived latency reduction.

### Barge-in / interruption

When the caller speaks while the bot is speaking, OpenAI Realtime's server-side VAD fires `input_audio_buffer.speech_started`. We respond by:

1. Sending `response.cancel` to OpenAI (stops generation)
2. Sending a `clear` event to Tata (drops queued audio frames in their jitter buffer)

Without (2) the caller hears the bot's tail even after they interrupt.

## Tool calling

OpenAI Realtime supports function calling natively. The bridge defines tools, the model emits `response.function_call_arguments.done`, and the bridge:

1. Looks up the handler in `tools.py`
2. Executes (HTTP call to `api` for CRM operations, or local logic)
3. Sends `conversation.item.create` with the result
4. Sends `response.create` to resume the conversation

Tools shipped:

- `lookup_customer(phone)` → name, account_status, recent_orders
- `schedule_appointment(date, time, service)` → confirmation_id
- `transfer_to_human(reason)` → ends bot session, Tata bridges to agent
- `end_call(reason)` → graceful hangup

## Session lifecycle

```
[Tata "start" event]
   │
   ▼
[Bridge creates Session, allocates call_id, opens OpenAI WS]
   │
   ▼
[Outbound only: GET /v1/campaigns/_contacts/<id>/bot_config → brand, voice,
 language, optional system_prompt_override]
   │
   ▼
[Bridge sends session.update with merged instructions + tools + voice]
   │
   ▼
[Bridge sends conversation.item.create with optional outbound greeting trigger]
   │
   ▼
[Three asyncio tasks race: tata→openai, openai→tata, watchdog]
   │
   ▼
[Watchdog enforces: max_call_duration, post-speech silence, initial silence;
 sets _wrap_up_cause and triggers teardown]
   │
   ▼
[On any task return / hangup / error: cancel siblings, persist transcript,
 close WSs, finalize Call.outcome with the watchdog's cause if it fired]
```

### Per-campaign bot configuration

Outbound campaigns can override the system prompt, brand, voice, and language
without redeploying the bridge. The flow:

1. Dashboard creates a Campaign with `voice="shimmer"`, `language="hinglish"`,
   etc. The API validates against a small allowlist.
2. Worker dials each contact via Tata's "click-to-call with streaming",
   passing `campaign_contact_id` in `customParameters`.
3. Tata streams audio to the bridge with that custom parameter intact.
4. The Tata adapter promotes `campaign_contact_id` into `CallContext.extra`.
5. The bridge fetches `/v1/campaigns/_contacts/<id>/bot_config` and merges
   any non-NULL fields with the bridge defaults before calling
   `session.update` on the Realtime WS.

`system_prompt_override` is an escape hatch — when set, it replaces the
structured prompt entirely. Otherwise `agent.build_system_prompt(brand=...,
language=...)` produces a localized prompt (English / Hinglish / Hindi
shipped today; new languages are a single dict entry in `agent.py`).

## Persistence

Transcripts are streamed to Postgres incrementally (one row per `conversation.item.input_audio_transcription.completed` and `response.audio_transcript.done` event), so a crash during a call doesn't lose history.

Recordings are optional (off by default for cost). When enabled, the bridge writes one μ-law file per track per call (caller + bot kept separate for downstream stereo merge / sentiment scoring). Two backends ship behind a common `Recorder` interface:

- `disk` — writes straight to `recordings/`. Dev / sidecar.
- `s3` — buffers to a temp file during the call, uploads on hangup. Optional SSE-KMS encryption (`RECORDINGS_S3_KMS_KEY_ID`). Object layout `s3://<bucket>/<prefix>/YYYY/MM/DD/<call_id>.<inbound|outbound>.ulaw` so per-day lifecycle rules (Glacier/expire) are one rule per bucket, no scanning.

Misconfiguring the S3 backend (empty bucket) falls back to `disk` with a loud log line — a config typo doesn't drop every recording in production.

## Data model

```
users           ─┐
                 │
calls          ──┤  ─< transcript_segments
                 │   ─< tool_invocations
                 │   ─< call_metrics (one row, denormalized)
                 │
campaigns      ──┤  ─< campaign_contacts ─< calls (FK)
                 │
contacts       ──┘
```

See `services/api/app/models.py` for the SQLAlchemy definitions.

## Observability

- **Logs**: `structlog` JSON to stdout. Every log line carries `call_id`, `request_id`, `service`. Aggregate with Loki / Datadog / CloudWatch.
- **Metrics**: Prometheus, exposed at `/metrics` on every service. Key metrics:
  - `bridge_calls_active` (gauge)
  - `bridge_call_duration_seconds` (histogram)
  - `bridge_first_response_latency_seconds` (histogram) — mic close → first audio out
  - `bridge_openai_errors_total` (counter, by error_code)
  - `bridge_audio_frames_total` (counter, by direction)
- **Traces**: OpenTelemetry hooks present (commented out — wire to your collector)

## Security

- All inter-service calls require a JWT (service-to-service: a long-lived service token; user-to-API: short-lived user token)
- Tata webhooks verified via HMAC-SHA256 signature in `X-Tata-Signature` header (configurable)
- Secrets from env only; `.env` is gitignored; `.env.example` is the source of truth for what's required
- Postgres connections use SSL when `DATABASE_SSL=require`
- The dashboard sets `Strict-Transport-Security`, `X-Frame-Options`, `Content-Security-Policy` (see `frontend/nginx.conf`)

## Scaling

- **bridge**: horizontal — sticky sessions not required (each call is its own WS pair). Scale on `bridge_calls_active`.
- **api**: horizontal, stateless. Scale on RPS.
- **worker**: horizontal. Scale on Redis queue depth.
- **postgres**: vertical first; read replicas for the dashboard if needed.
- **redis**: single instance is fine for tens of thousands of campaigns; cluster only at scale.

## Deployment

`docker-compose.yml` is for development. For production:

- Each service has a `Dockerfile` that produces a minimal image
- Drop into Kubernetes with the manifests in `deploy/k8s/` (TODO — not in MVP)
- TLS termination at the ingress (Caddy, Nginx Ingress, or AWS ALB)
- Postgres and Redis as managed services (RDS / ElastiCache)
- See [`docs/deployment.md`](./docs/deployment.md)

## Outbound campaign reliability

Three pieces work together so a campaign actually completes 100% of its
contacts even when workers crash and carriers misbehave:

1. **`SELECT ... FOR UPDATE SKIP LOCKED`** in `claim_next` so multiple
   workers can run concurrently without claiming the same row.
2. **`scheduled_at` gate** — a campaign flipped to `running` ahead of time
   doesn't start dialing until the scheduled moment.
3. **Retry-or-abandon on failure**: every transition out of `dialing`
   (worker `_complete`, Tata webhook `failed`/`busy`/`no_answer`, or the
   sweeper finding a stale row) goes through `_apply_failure(contact,
   campaign, error)` which checks `attempts < retry_attempts + 1` and
   returns to `pending` for retry, else terminal `failed`.
4. **Stale-`dialing` sweeper** — `POST /v1/campaigns/_sweep_stale` reverts
   any `dialing` row older than `older_than_seconds`. The worker calls
   this once per loop iteration before claiming new work; this recovers
   contacts orphaned by a worker crash mid-dial in well under a minute.

## What's intentionally NOT in this MVP

- **Multi-tenant isolation** (single tenant per deployment)
- **SIP trunk auth** (we rely on Tata's WS auth)
- **In-call sentiment analysis** (post-call analysis runs in `worker`; live sentiment is straightforward to add but adds latency)
- **Kubernetes manifests** (Compose works; K8s is mechanical from there)
- **Stereo WAV merge of recording tracks** (we ship two μ-law files per call; merging into a stereo WAV is a downstream batch job)

These are clean extension points, not architectural blockers.
