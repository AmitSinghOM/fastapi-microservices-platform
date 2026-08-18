"""Separately runnable async webhook delivery worker: python -m app.worker."""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from app.config import Settings, get_settings
from app.db import create_database_resources
from app.services.delivery_service import ClaimedDelivery, DeliveryService

logger = logging.getLogger(__name__)


async def _run_claim(
    service: DeliveryService, claim: ClaimedDelivery
) -> None:
    try:
        await service.deliver(claim)
    except asyncio.CancelledError:
        await asyncio.shield(service.release_claim(claim))
        raise
    except Exception as exc:
        logger.error(
            "Unexpected worker error for delivery %s (%s)",
            claim.public_id,
            type(exc).__name__,
        )
        await service.release_claim(claim)


async def run_delivery_loop(
    service: DeliveryService,
    settings: Settings,
    stop: asyncio.Event,
) -> None:
    """Claim only free execution slots, then drain bounded work on stop."""
    in_flight: set[asyncio.Task[None]] = set()
    while not stop.is_set():
        in_flight = {task for task in in_flight if not task.done()}
        available = settings.worker_concurrency - len(in_flight)
        if available > 0:
            claims = await service.claim_due(
                min(settings.worker_batch_size, available)
            )
            if stop.is_set():
                await asyncio.gather(
                    *(service.release_claim(claim) for claim in claims)
                )
                break
            for claim in claims:
                in_flight.add(asyncio.create_task(_run_claim(service, claim)))
            if claims:
                continue
        if in_flight:
            await asyncio.wait(
                in_flight,
                timeout=settings.worker_poll_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.worker_poll_seconds
                )
            except TimeoutError:
                pass

    if not in_flight:
        return
    _, pending = await asyncio.wait(
        in_flight,
        timeout=settings.worker_shutdown_grace_seconds,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*in_flight, return_exceptions=True)


async def run_worker() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
            installed_signals.append(signal_name)
        except NotImplementedError:
            pass

    timeout = httpx.Timeout(
        connect=settings.http_connect_timeout_seconds,
        read=settings.http_read_timeout_seconds,
        write=settings.http_write_timeout_seconds,
        pool=settings.http_pool_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=settings.worker_concurrency,
        max_keepalive_connections=settings.worker_concurrency,
    )
    database = create_database_resources(settings, "worker")
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            service = DeliveryService(
                database.session_factory, client, settings
            )
            logger.info("Webhook worker started")
            await run_delivery_loop(service, settings, stop)
    finally:
        for signal_name in installed_signals:
            loop.remove_signal_handler(signal_name)
        await database.close()
    logger.info("Webhook worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
