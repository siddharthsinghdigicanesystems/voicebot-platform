# Tata SmartFlo integration

This guide takes you from local mock-telephony demo to live calls on Tata SmartFlo Voice Streaming.

## Prerequisites

- A Tata SmartFlo account with **Voice Streaming** enabled (request from your Tata account manager — it isn't on by default).
- At least one **DID number** provisioned on the account, with both inbound IVR and outbound dialing permissions.
- A public HTTPS URL for the bridge (TLS termination at Caddy/Nginx; Tata won't connect over plain `ws://` in production).
- A public HTTPS URL for the api service (for webhooks).

## Configure Tata

Tata's panel calls these "Stream Trigger" actions in the IVR Studio. Using the panel:

1. **Create the streaming destination**
   - In SmartFlo IVR Studio, drag in a "Stream" / "Voice Streaming" node.
   - WebSocket URL: `wss://YOUR-DOMAIN/v1/telephony/tata` (aliases `wss://YOUR-DOMAIN/websocket` and `wss://YOUR-DOMAIN/` route to the same handler — use whichever your account's panel accepts).
   - Audio format: `audio/x-mulaw`, sample rate `8000`, mono.
   - Auth: Bearer header — set the bearer to a strong random secret. Put the same secret in `.env` as `TATA_STREAMING_AUTH_TOKEN`.
   - In "Custom parameters", set:
     - `direction = inbound`
     - `from = {caller_number}` (Tata variable for the calling party)
     - `to = {dialed_number}`
   - Bind this Stream node to your inbound IVR flow (typically: greet → "press 1 to talk to our assistant" → Stream).

2. **Configure webhooks**
   - In SmartFlo → Settings → Webhooks, add:
     - URL: `https://YOUR-DOMAIN/v1/webhooks/tata`
     - Events: `call.answered`, `call.hangup`, `call.failed`, `call.no_answer`, `call.busy`
     - HMAC secret: a strong random value. Set `TATA_WEBHOOK_SECRET` in `.env` to the same value.

3. **For outbound calls** — set up a "Click to Call with Streaming" template
   - The worker calls Tata's outbound API (`/v1/calls/outbound` in `services/worker/app/tata_client.py`).
   - Confirm with your Tata account manager which API endpoint and request shape your account uses (they vary by region/version). Adjust `TataClient.dial()` if needed.
   - Set `TATA_OUTBOUND_CALLER_ID` in `.env` to one of your provisioned DIDs.

## Configure the platform

```bash
# .env
TELEPHONY_ADAPTER=tata
TATA_STREAMING_AUTH_TOKEN=<the bearer you set above>
TATA_WEBHOOK_SECRET=<the HMAC secret you set above>
TATA_API_KEY=<your Tata API token>
TATA_API_BASE_URL=https://api-smartflo.tatateleservices.com
TATA_OUTBOUND_CALLER_ID=<your DID, e.g. 04412345678>
```

## Streaming protocol (what the bridge implements)

Smartflo opens the WSS connection to us (we are the server); the connection is
full duplex. Audio is **always** G.711 μ-law, 8000 Hz, 8-bit, mono,
base64-encoded — both directions, no exceptions (`services/bridge/app/telephony/tata.py`).

Lifecycle: `connected` → `start` → `media`…`media` → `stop` → close.

Events received and how the bridge handles them:

| Event | Handling |
|---|---|
| `connected` | Handshake; logged, no processing. |
| `start` | Creates the session. `streamSid` is the primary key; we store `callSid`, `accountSid`, `from`, `to`, and the dynamic `customParameters` verbatim. |
| `media` | Base64 → μ-law bytes → forwarded to speech recognition / LLM / TTS. `chunk`/`timestamp`/`sequenceNumber` are logged (never the payload). |
| `dtmf` | Digit (`0`-`9`, `*`, `#`, `A`-`D`) forwarded into the conversation engine. |
| `mark` | Playback acknowledgement — resolves the matching outstanding mark we sent. |
| `stop` | Releases the session, flushes buffers, closes the socket. Reason is informational. |
| _unknown_ | Logged and ignored — never crashes the connection. |

Events sent to Smartflo (only these three): `media` (μ-law bot audio),
`mark` (sent right after each bot turn; Smartflo echoes it when playback
finishes), and `clear` (barge-in — discards Smartflo's buffered bot audio).

Robustness: malformed JSON, a missing/mismatched `streamSid`, and undecodable
audio are all logged and skipped; a single bad frame never tears down the call
or the process. No application-level auth is assumed — transport security is
TLS (`wss://`). The optional `TATA_STREAMING_AUTH_TOKEN` bearer check is an
extra, off by default.

## Verify

1. **Inbound** — call your DID from a real phone:
   - Tata's panel should show the Stream node firing.
   - Bridge logs should show `tata.start` with the call SID.
   - Within ~600 ms of you finishing your first sentence, the bot speaks back.
   - The dashboard at `/calls/<id>` should show the live transcript.

2. **Outbound** — create a campaign in the dashboard with one phone number, click Start:
   - Worker logs should show `dial.start` and `dial.done` with `success=true`.
   - Your phone rings; on answer, the bot greets you by name (if seeded in the CRM).

## Troubleshooting

- **"connection refused" in Tata panel** — the bridge isn't reachable on `wss://`. Check your reverse proxy and TLS cert.
- **Audio is one-way** — most likely a format mismatch. Confirm Tata's panel says `audio/x-mulaw @ 8000`. The bridge expects μ-law @ 8 kHz.
- **Bot speaks late or stutters** — check `bridge_first_response_latency_seconds` and `bridge_audio_frames_total`. If frames are bursty, your reverse proxy is buffering — disable proxy buffering for the WS path.
- **Webhook 401 from Tata's panel** — the HMAC secret in `.env` doesn't match the one configured in Tata. Tata's signature header is typically `X-Tata-Signature: sha256=<hex>`.
- **Outbound dials are 404 / 401** — your account's outbound API endpoint differs from the default. Open `services/worker/app/tata_client.py` and adjust `_client.post(...)`.

## Cutover plan

1. Deploy with `TELEPHONY_ADAPTER=tata` to staging.
2. Use a single test DID, route only your QA team's calls there.
3. Run `make logs` and watch:
   - `bridge_first_response_latency_seconds` p50 < 700 ms
   - `bridge_openai_errors_total` near zero
   - `bridge_calls_total{outcome="error"}` near zero
4. Promote to your customer-facing DIDs.
