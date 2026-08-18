"""Separately runnable async webhook delivery worker: python -m app.worker."""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from app.config import get_settings
from app.db import async_session, close_database
from app.services.delivery_service import ClaimedDelivery, DeliveryService

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop.set)
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
    semaphore = asyncio.Semaphore(settings.worker_concurrency)
    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        service = DeliveryService(async_session, client, settings)

        async def deliver(claim: ClaimedDelivery) -> None:
            async with semaphore:
                try:
                    await service.deliver(claim)
                except Exception as exc:
                    logger.error(
                        "Unexpected worker error for delivery %s (%s)",
                        claim.public_id,
                        type(exc).__name__,
                    )

        logger.info("Webhook worker started")
        while not stop.is_set():
            claims = await service.claim_due(settings.worker_batch_size)
            if claims:
                await asyncio.gather(*(deliver(claim) for claim in claims))
                continue
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=settings.worker_poll_seconds
                )
            except TimeoutError:
                pass
    await close_database()
    logger.info("Webhook worker stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
