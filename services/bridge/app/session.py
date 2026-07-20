"""Call session orchestrator — the heart of the bridge.

One `Session.run()` per call. Three asyncio tasks fan out:

  caller_to_bot  : reads μ-law frames from the telephony adapter, forwards
                   to OpenAI Realtime as input_audio_buffer.append.
  bot_to_caller  : reads events from OpenAI, dispatches:
                       response.output_audio.delta -> telephony.send_audio
                       function_call_arguments  -> tools.dispatch + submit
                       speech_started           -> barge-in: cancel + clear
                       transcripts              -> persist + (optional) pubsub
  watchdog       : enforces hard call-duration cap and silence timeouts so
                   a hung WS / muted caller / runaway model can never burn
                   minutes forever. Sets `_wrap_up_cause` and triggers
                   teardown via the same first-completed race.

Lifecycle is straight-line: setup -> race the three tasks -> teardown.
Tools that set ctx.transfer_destination or ctx.end_call cause the
orchestrator to drain and finish the call cleanly.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any

from app.agent import TOOL_SCHEMAS, build_initial_user_hint, build_system_prompt
from app.api_client import ApiClient
from app.config import settings
from app.logging_setup import call_id_var, get_logger
from app.metrics import (
    audio_frames_total,
    call_duration_seconds,
    calls_active,
    calls_total,
    first_response_latency_seconds,
    openai_errors_total,
)
from app.openai_realtime import OpenAIRealtimeClient
from app.persistence import (
    LiveTranscriptPublisher,
    Recorder,
    make_recorder,
    persist_call_end,
    persist_call_start,
    persist_tool_invocation,
    persist_transcript_segment,
)
from app.telephony.base import CallContext, CallDirection, TelephonyAdapter
from app.tools import ToolContext, dispatch

log = get_logger(__name__)


class Session:
    """Owns one call from `start` to `stop`."""

    def __init__(self, telephony: TelephonyAdapter) -> None:
        self.telephony = telephony
        self.openai = OpenAIRealtimeClient()
        self.call_ctx: CallContext | None = None
        self.tool_ctx: ToolContext | None = None
        self.api: ApiClient | None = None
        self.recorder: Recorder | None = None
        self.live_publisher: LiveTranscriptPublisher | None = None

        self._started_at: float = 0.0
        self._caller_speech_ended_at: float | None = None
        self._first_response_recorded: bool = False
        self._pending_function_calls: dict[str, dict[str, Any]] = {}
        self._stop = asyncio.Event()
        self._db_call_id: str | None = None
        # Monotonic counter for the unique playback-mark labels we send after
        # each bot turn (so we can correlate the carrier's mark ack back to us).
        self._mark_seq: int = 0

        # Watchdog state
        self._last_caller_frame_at: float = 0.0
        self._bot_has_spoken: bool = False
        # Cause set by the watchdog when it forces a wrap-up. Used to record
        # an accurate `outcome` and to skip the bot's would-be next turn.
        self._wrap_up_cause: str | None = None

    # --- entrypoint -----------------------------------------------------------

    async def run(self) -> None:
        ctx = await self.telephony.receive_call()
        self.call_ctx = ctx
        # Surface out-of-band caller events (keypad + playback acks) into the
        # conversation. Wired here, before the audio pumps start, so the
        # adapter's receive loop can invoke them.
        self.telephony.on_dtmf = self._on_caller_dtmf
        self.telephony.on_mark = self._on_playback_mark
        call_id_var.set(ctx.provider_call_id)
        log.info(
            "session.start",
            direction=ctx.direction,
            from_=ctx.from_number,
            to=ctx.to_number,
        )

        calls_active.inc()
        self._started_at = time.monotonic()
        self._last_caller_frame_at = self._started_at
        outcome = "completed"

        try:
            async with ApiClient() as api:
                self.api = api
                self.tool_ctx = ToolContext(call=ctx, api=api)

                await self._setup_persistence()
                await self._setup_openai()

                # Race the three pumps. The watchdog acts as a deadline + idle
                # detector and is the only one that can force-end the session
                # without either side hanging up. If any pump returns first we
                # cancel the others and tear down promptly.
                tasks = [
                    asyncio.create_task(self._pump_caller_to_bot(), name="caller_to_bot"),
                    asyncio.create_task(self._pump_bot_to_caller(), name="bot_to_caller"),
                    asyncio.create_task(self._pump_watchdog(), name="watchdog"),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                # Surface exceptions from the completed task; swallow CancelledError
                # from cancelled siblings.
                for t in done:
                    if t.exception() is not None:
                        raise t.exception()  # type: ignore[misc]
                for t in pending:
                    with suppress(asyncio.CancelledError, Exception):
                        await t

                if self._wrap_up_cause:
                    outcome = self._wrap_up_cause
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("session.error", error=str(exc))
            outcome = "error"
        finally:
            duration = time.monotonic() - self._started_at
            call_duration_seconds.observe(duration)
            calls_active.dec()
            calls_total.labels(direction=ctx.direction.value, outcome=outcome).inc()
            await self._teardown(outcome=outcome, duration_s=duration)

    # --- setup ----------------------------------------------------------------

    async def _setup_persistence(self) -> None:
        assert self.api is not None and self.call_ctx is not None
        record = await persist_call_start(self.api, self.call_ctx)
        self._db_call_id = record["id"]

        if settings.enable_recordings:
            self.recorder = make_recorder(self.call_ctx.provider_call_id)
            await self.recorder.open()

        if settings.enable_live_transcript_pubsub:
            self.live_publisher = LiveTranscriptPublisher(self._db_call_id)
            await self.live_publisher.connect()

    async def _setup_openai(self) -> None:
        assert self.call_ctx is not None
        await self.openai.connect()

        # --- per-campaign bot overrides --------------------------------------
        # On outbound calls the worker passed `campaign_contact_id` through in
        # the carrier's customParameters. We fetch the campaign's bot config
        # and merge into the bridge defaults: NULL fields ⇒ default. A 404
        # (contact gone) silently falls back too — never fail a live call on
        # a config lookup.
        bot_brand: str = "Acme Health"
        bot_language: str = "en"
        bot_voice: str = settings.openai_voice
        bot_prompt_override: str | None = None

        contact_id = self.call_ctx.extra.get("campaign_contact_id")
        if (
            self.call_ctx.direction == CallDirection.OUTBOUND
            and contact_id
            and self.api is not None
        ):
            with suppress(Exception):
                cfg = await self.api.get_contact_bot_config(contact_id)
                if cfg:
                    if cfg.get("brand"):
                        bot_brand = cfg["brand"]
                    if cfg.get("language"):
                        bot_language = cfg["language"]
                    if cfg.get("voice"):
                        bot_voice = cfg["voice"]
                    if cfg.get("system_prompt_override"):
                        bot_prompt_override = cfg["system_prompt_override"]
                    log.info(
                        "session.bot_config_loaded",
                        contact_id=contact_id,
                        language=bot_language,
                        voice=bot_voice,
                        prompt_override=bool(bot_prompt_override),
                    )

        # --- pre-fetch customer for outbound greeting ------------------------
        outbound_hint: str | None = None
        if self.call_ctx.direction == CallDirection.OUTBOUND and self.api is not None:
            customer = await self.api.lookup_customer(self.call_ctx.to_number)
            if customer:
                assert self.tool_ctx is not None
                self.tool_ctx.customer = customer
                outbound_hint = build_initial_user_hint(
                    self.call_ctx.direction,
                    customer_name=customer.get("name"),
                    appointment=customer.get("next_appointment"),
                )

        instructions = bot_prompt_override or build_system_prompt(
            self.call_ctx.direction,
            brand=bot_brand,
            language=bot_language,
        )

        await self.openai.configure_session(
            instructions=instructions,
            voice=bot_voice,
            tools=TOOL_SCHEMAS,
            input_audio_format=settings.audio_format,
            output_audio_format=settings.audio_format,
        )

        if self.call_ctx.direction == CallDirection.OUTBOUND:
            await self.openai.trigger_initial_greeting(outbound_hint)

    # --- audio pumps ----------------------------------------------------------

    async def _pump_caller_to_bot(self) -> None:
        async for frame in self.telephony.receive_audio():
            self._last_caller_frame_at = time.monotonic()
            audio_frames_total.labels(direction="caller_to_bot").inc()
            if self.recorder:
                await self.recorder.write_inbound(frame)
            try:
                await self.openai.send_audio(frame)
            except Exception as exc:  # noqa: BLE001
                log.warning("session.openai_send_failed", error=str(exc))
                break
        log.info("session.caller_audio_eof")
        self._stop.set()

    async def _pump_bot_to_caller(self) -> None:
        # When a tool sets end_call or transfer_destination, we don't tear
        # the call down right away — the model still owes the caller a
        # spoken goodbye / "I'll connect you" message. submit_tool_result
        # has already triggered a fresh response.create, so we keep pumping
        # audio until that response's response.done arrives (then a tiny
        # drain in _wrap_up flushes the carrier).
        #
        # If the model hangs and no events arrive, we still need to bail out;
        # the grace timeout below is the safety cap for that. We get it for
        # free even with no incoming events because we wrap each event read
        # in asyncio.wait_for once wrap-up is pending.
        wrap_up_pending_at: float | None = None
        saw_post_tool_response_created = False

        events_iter = self.openai.events().__aiter__()
        while not self._stop.is_set():
            timeout: float | None = None
            if wrap_up_pending_at is not None:
                timeout = max(
                    0.001,
                    (wrap_up_pending_at + settings.wrap_up_grace_seconds)
                    - time.monotonic(),
                )

            try:
                event = await asyncio.wait_for(
                    events_iter.__anext__(), timeout=timeout
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - (wrap_up_pending_at or 0.0)
                log.warning(
                    "session.wrap_up_grace_exceeded",
                    elapsed=round(elapsed, 2),
                    grace=settings.wrap_up_grace_seconds,
                )
                await self._wrap_up()
                break

            await self._handle_openai_event(event)

            if self.tool_ctx and (self.tool_ctx.end_call or self.tool_ctx.transfer_destination):
                etype = event.get("type", "")
                if wrap_up_pending_at is None:
                    wrap_up_pending_at = time.monotonic()
                    log.info(
                        "session.wrap_up_pending",
                        reason="end_call" if self.tool_ctx.end_call else "transfer",
                    )
                if etype == "response.created":
                    saw_post_tool_response_created = True

                if etype == "response.done" and saw_post_tool_response_created:
                    await self._wrap_up()
                    break

    async def _pump_watchdog(self) -> None:
        """Enforce hard duration cap and silence timeouts.

        Runs at 1 Hz; cheap. Three triggers, in priority order:

          1. Hard duration cap: end the call regardless of state.
          2. Mid-conversation silence: bot has spoken at least once and we
             haven't heard a caller frame in `caller_silence_timeout_seconds`.
             Common causes: caller hung up but the carrier didn't tell us, WS
             link wedged, or the caller put us on hold. Either way we're
             billing minutes for nothing.
          3. Initial silence: caller never sent any media frames within the
             `caller_initial_silence_timeout_seconds` window. The mic-mute
             scenario, basically.

        On any trigger we set `_wrap_up_cause` (used as the persisted
        outcome) and return — the orchestrator's `asyncio.wait` will then
        cancel the audio pumps and run teardown.
        """
        max_dur = settings.max_call_duration_seconds
        idle_timeout = settings.caller_silence_timeout_seconds
        initial_idle_timeout = settings.caller_initial_silence_timeout_seconds

        while not self._stop.is_set():
            await asyncio.sleep(1.0)

            now = time.monotonic()
            elapsed = now - self._started_at
            since_last_caller_frame = now - self._last_caller_frame_at

            if elapsed >= max_dur:
                log.warning(
                    "session.watchdog.duration_cap",
                    elapsed=round(elapsed, 1),
                    cap=max_dur,
                )
                self._wrap_up_cause = "duration_cap"
                with suppress(Exception):
                    await self.telephony.hangup()
                self._stop.set()
                return

            if self._bot_has_spoken and since_last_caller_frame >= idle_timeout:
                log.warning(
                    "session.watchdog.caller_silent",
                    silent_for=round(since_last_caller_frame, 1),
                    timeout=idle_timeout,
                )
                self._wrap_up_cause = "caller_silent"
                with suppress(Exception):
                    await self.telephony.hangup()
                self._stop.set()
                return

            if not self._bot_has_spoken and elapsed >= initial_idle_timeout:
                # Caller never spoke and bot was meant to greet first (outbound)
                # or caller is muted (inbound). Either way, give up.
                log.warning(
                    "session.watchdog.initial_silence",
                    elapsed=round(elapsed, 1),
                    timeout=initial_idle_timeout,
                )
                self._wrap_up_cause = "initial_silence"
                with suppress(Exception):
                    await self.telephony.hangup()
                self._stop.set()
                return

    # --- event dispatch -------------------------------------------------------

    async def _handle_openai_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        # GA renamed several response.* events; accept both names during migration.
        if etype in ("response.output_audio.delta", "response.audio.delta"):
            await self._handle_audio_delta(event)
        elif etype == "input_audio_buffer.speech_started":
            await self._handle_user_started_speaking()
        elif etype == "input_audio_buffer.speech_stopped":
            self._caller_speech_ended_at = time.monotonic()
        elif etype == "conversation.item.input_audio_transcription.completed":
            await self._handle_user_transcript(event)
        elif etype in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
            await self._handle_bot_transcript(event)
        elif etype == "response.function_call_arguments.delta":
            self._accumulate_function_call(event)
        elif etype == "response.function_call_arguments.done":
            await self._handle_function_call_done(event)
        elif etype in ("response.output_audio.done", "response.audio.done"):
            # The model finished streaming a full audio turn; tag the tail with
            # a mark so Smartflo tells us when the caller has actually heard it.
            await self._send_playback_mark()
        elif etype == "error":
            err = event.get("error", {})
            code = str(err.get("code", "unknown"))
            openai_errors_total.labels(code=code).inc()
            log.error("openai.error", code=code, message=err.get("message"))
        elif etype == "response.done":
            log.debug("openai.response_done")
        else:
            log.debug("openai.unhandled_event", type=etype)

    # --- handlers -------------------------------------------------------------

    async def _handle_audio_delta(self, event: dict[str, Any]) -> None:
        import base64

        b64 = event.get("delta", "")
        if not b64:
            return
        frame = base64.b64decode(b64)
        if self.recorder:
            await self.recorder.write_outbound(frame)

        self._bot_has_spoken = True

        if not self._first_response_recorded and self._caller_speech_ended_at is not None:
            latency = time.monotonic() - self._caller_speech_ended_at
            first_response_latency_seconds.observe(latency)
            self._first_response_recorded = True
            log.info("session.first_response_latency", seconds=round(latency, 3))

        try:
            await self.telephony.send_audio(frame)
            audio_frames_total.labels(direction="bot_to_caller").inc()
        except Exception as exc:  # noqa: BLE001
            log.warning("session.telephony_send_failed", error=str(exc))

    async def _send_playback_mark(self) -> None:
        """Send a uniquely-labelled mark right after a bot audio turn.

        Smartflo echoes the label back (via `_on_playback_mark`) once it has
        finished playing that audio to the caller — our synchronization point
        for advancing queued responses / conversation state.
        """
        self._mark_seq += 1
        label = f"bot-turn-{self._mark_seq}"
        with suppress(Exception):
            await self.telephony.send_mark(label)

    async def _on_caller_dtmf(self, digit: str) -> None:
        """Forward a caller keypad press into the conversation engine."""
        log.info("session.dtmf", digit=digit)
        if not self.openai.connected:
            return
        with suppress(Exception):
            await self.openai.send_user_text(
                f"The caller pressed the phone keypad digit: {digit}."
            )

    async def _on_playback_mark(self, name: str) -> None:
        """Playback acknowledgement: Smartflo finished playing a bot turn."""
        log.info("session.playback_complete", mark=name)

    async def _handle_user_started_speaking(self) -> None:
        """Barge-in: cancel the model's current response and flush carrier audio."""
        log.info("session.barge_in")
        self._first_response_recorded = False
        self._caller_speech_ended_at = None
        with suppress(Exception):
            await self.openai.cancel_response()
        with suppress(Exception):
            await self.telephony.clear_buffer()

    async def _handle_user_transcript(self, event: dict[str, Any]) -> None:
        text = (event.get("transcript") or "").strip()
        if not text or not self._db_call_id or not self.api:
            return
        log.info("transcript.user", text=text)
        await persist_transcript_segment(
            self.api,
            self._db_call_id,
            role="user",
            text=text,
            item_id=event.get("item_id"),
        )
        if self.live_publisher:
            await self.live_publisher.publish(role="user", text=text)

    async def _handle_bot_transcript(self, event: dict[str, Any]) -> None:
        text = (event.get("transcript") or "").strip()
        if not text or not self._db_call_id or not self.api:
            return
        log.info("transcript.bot", text=text)
        await persist_transcript_segment(
            self.api,
            self._db_call_id,
            role="assistant",
            text=text,
            item_id=event.get("item_id"),
        )
        if self.live_publisher:
            await self.live_publisher.publish(role="assistant", text=text)

    def _accumulate_function_call(self, event: dict[str, Any]) -> None:
        call_id = event.get("call_id")
        if not call_id:
            return
        slot = self._pending_function_calls.setdefault(
            call_id, {"name": event.get("name", ""), "args": ""}
        )
        if event.get("name"):
            slot["name"] = event["name"]
        slot["args"] += event.get("delta", "")

    async def _handle_function_call_done(self, event: dict[str, Any]) -> None:
        call_id = event.get("call_id")
        name = event.get("name") or self._pending_function_calls.get(call_id, {}).get("name", "")
        raw_args = event.get("arguments") or self._pending_function_calls.get(call_id, {}).get(
            "args", "{}"
        )
        self._pending_function_calls.pop(call_id, None)

        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            log.warning("tool.bad_json", name=name, raw=raw_args)
            args = {}

        log.info("tool.invoke", name=name, args=args)
        assert self.tool_ctx is not None
        result = await dispatch(name, args, self.tool_ctx)

        if self._db_call_id and self.api:
            await persist_tool_invocation(
                self.api,
                self._db_call_id,
                name=name,
                arguments=args,
                result=result,
            )

        await self.openai.submit_tool_result(call_id or "", result)

    # --- wrap-up --------------------------------------------------------------

    async def _wrap_up(self) -> None:
        """Hand off to transfer/hangup. The bot pump has already waited for the
        model's goodbye to finish; we just flush the last in-flight audio frame
        before the carrier-side action."""
        assert self.tool_ctx is not None
        # tiny drain — last audio frame may still be on the wire to the carrier
        await asyncio.sleep(0.3)
        if self.tool_ctx.transfer_destination:
            log.info("session.transfer", destination=self.tool_ctx.transfer_destination)
            with suppress(Exception):
                await self.telephony.transfer(self.tool_ctx.transfer_destination)
        else:
            log.info("session.end_call", reason=self.tool_ctx.end_reason)
            with suppress(Exception):
                await self.telephony.hangup()
        self._stop.set()

    # --- teardown -------------------------------------------------------------

    async def _teardown(self, *, outcome: str, duration_s: float) -> None:
        with suppress(Exception):
            await self.openai.close()
        with suppress(Exception):
            await self.telephony.close()

        if self.recorder:
            await self.recorder.close()
        if self.live_publisher:
            await self.live_publisher.close()

        if self._db_call_id and self.api is not None:
            with suppress(Exception):
                await persist_call_end(
                    self.api,
                    self._db_call_id,
                    outcome=outcome,
                    duration_s=duration_s,
                    facts=(self.tool_ctx.facts if self.tool_ctx else {}),
                )

        log.info("session.end", outcome=outcome, duration_s=round(duration_s, 2))
