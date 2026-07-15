# ADR 0001 — Use OpenAI Realtime API instead of a chained STT→LLM→TTS pipeline

**Date:** 2026-05-07
**Status:** Accepted

## Context

Voice bots have historically been built as a pipeline:

```
audio → VAD → STT → LLM → TTS → audio
```

Each hop adds latency. With cloud APIs:

- VAD: ~50 ms
- STT (Whisper batch): ~250–600 ms
- LLM (GPT-4o): ~300–600 ms first-token
- TTS first-byte: ~200–400 ms

Total perceptible delay before the bot speaks: **~800–1600 ms**. That feels robotic.

OpenAI's Realtime API does the entire loop inside one model with a single WebSocket. It also handles VAD server-side and supports native barge-in.

## Decision

Use the OpenAI Realtime API for STT + LLM + TTS as a single hop. Default to the current GA snapshot (`gpt-realtime-2025-08-28` as of May 2026) rather than a rolling alias, so deploys are reproducible. `gpt-realtime-2` is available for higher-reasoning use cases (128k context window) and `gpt-realtime-1.5` for cost-sensitive lower-latency campaigns; the bridge selects per-campaign via the `bot_config` endpoint.

## Consequences

**Positive:**

- Measured first-response latency: ~400–600 ms (mic close → first audio out).
- Native barge-in via server VAD events (`input_audio_buffer.speech_started`).
- Simpler bridge code: one WS in, one WS out, no orchestration of three providers.
- One vendor SLA, one billing line item.

**Negative:**

- Hindi/regional language quality is weaker than Indic-tuned providers (Sarvam, Bhashini). For India deployments where Hindi is critical, this is a significant trade-off.
- Single point of failure: if OpenAI Realtime is down, calls can't be served. Mitigation: a fallback chained pipeline behind a circuit breaker.
- Cost is higher per minute than a self-hosted Whisper + Llama + Coqui stack. Mitigation: route low-stakes calls (e.g. simple appointment reminders) through a cheaper path.
- The Realtime API was in preview when this ADR was first written; it has since reached GA (`gpt-realtime`, Aug 2025). Event names and session schema in our `services/bridge/app/openai_realtime.py` remain compatible with the GA event surface (`session.update`, `input_audio_buffer.append`, `response.audio.delta`, `response.cancel`, `response.function_call_arguments.{delta,done}`, server-VAD `speech_started/stopped`).

## Alternatives considered

- **Self-hosted Whisper + vLLM (Llama-3.x) + Piper** — best for privacy/cost, but adds 200–400 ms latency vs. Realtime, and requires GPU infra. Reasonable for a v2.
- **Sarvam AI / Bhashini for Indic STT+TTS + GPT-4o for LLM** — best Hindi quality. Will be added as a second adapter when the user-language requirements expand.
- **Twilio AI / Deepgram Voice Agent** — comparable to OpenAI Realtime; vendor lock-in trade-off is the same. We chose OpenAI because the user explicitly selected it.
