"""Outbound campaign worker.

Loop:
  - poll the API for running campaigns
  - for each, claim up to `max_concurrency` pending contacts
  - dial each via the Tata client (async, capped concurrency)
  - mark each as succeeded/failed when the dial returns
  - sleep, repeat

Crash safety: a worker that dies mid-dial leaves contacts in `dialing`
state. The api service can sweep those back to `pending` after a
configurable timeout (TODO — the sweep is a single SQL update; trivial
to add when needed).
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from app.api_client import ApiClient
from app.config import settings
from app.logging_setup import configure_logging, get_logger
from app.tata_client import TataClient

configure_logging(settings.log_level)
log = get_logger(__name__)

_stop_evt = asyncio.Event()


async def _process_campaign(
    campaign: dict[str, Any], api: ApiClient, tata: TataClient
) -> None:
    cid = campaign["id"]
    sem = asyncio.Semaphore(min(campaign.get("max_concurrency", 5), settings.per_campaign_concurrency))
    tasks: list[asyncio.Task[None]] = []

    while not _stop_evt.is_set():
        contact = await api.claim_next(cid)
        if contact is None:
            break

        async def _dial(c: dict[str, Any]) -> None:
            async with sem:
                log.info("dial.start", contact_id=c["id"], phone=c["phone"])
                res = await tata.dial(
                    to=c["phone"],
                    campaign_contact_id=c["id"],
                    custom_parameters={"name": c.get("name") or ""},
                )
                await api.complete_contact(c["id"], success=res.success, error=res.error)
                log.info(
                    "dial.done",
                    contact_id=c["id"],
                    success=res.success,
                    provider_call_id=res.provider_call_id,
                    error=res.error,
                )

        tasks.append(asyncio.create_task(_dial(contact)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _loop() -> None:
    api = ApiClient()
    tata = TataClient()
    log.info(
        "worker.start",
        api=settings.api_internal_url,
        tata_configured=bool(settings.tata_api_key),
    )
    try:
        while not _stop_evt.is_set():
            # Janitor first: recover stale 'dialing' rows from a previous
            # crash before claiming new work. If this throws, keep going —
            # a sweep failure must never block live dialing.
            try:
                swept = await api.sweep_stale(settings.sweep_stale_older_than_seconds)
                if swept.get("requeued", 0) or swept.get("abandoned", 0):
                    log.info("worker.swept", **swept)
            except Exception as exc:  # noqa: BLE001
                log.warning("worker.sweep_failed", error=str(exc))

            try:
                running = await api.list_running_campaigns()
            except Exception as exc:  # noqa: BLE001
                log.warning("worker.list_failed", error=str(exc))
                running = []

            if running:
                await asyncio.gather(
                    *(_process_campaign(c, api, tata) for c in running),
                    return_exceptions=True,
                )
            try:
                await asyncio.wait_for(_stop_evt.wait(), timeout=settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        await asyncio.gather(api.aclose(), tata.aclose(), return_exceptions=True)
        log.info("worker.stop")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handler() -> None:
        log.info("worker.signal")
        _stop_evt.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: _stop_evt.set())


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
