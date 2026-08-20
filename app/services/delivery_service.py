from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Delivery, DeliveryAttempt
from app.scheduling import select_fair_deliveries
from app.security_observability import (
    SecurityDenyReason,
    SecurityLayer,
    record_security_deny,
)
from app.webhook_security import (
    UnsafeWebhookUrl,
    endpoint_secret,
    sign_payload,
    validate_webhook_url,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def database_now(session: AsyncSession) -> datetime:
    """Read authoritative lease time from the database server."""
    if session.get_bind().dialect.name == "sqlite":
        # SQLite CURRENT_TIMESTAMP has only whole-second precision. Using it
        # would make a newly enqueued row temporarily appear not due.
        return utcnow()
    value = await session.scalar(select(func.now()))
    if value is None:
        raise RuntimeError("Database did not return its current time")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class ClaimedDelivery:
    id: int
    public_id: str
    organization_id: int
    endpoint_id: int
    lease_token: str
    attempt_number: int
    endpoint_public_id: str
    endpoint_url: str
    endpoint_secret_version: int
    endpoint_active: bool
    event_public_id: str
    event_type: str
    canonical_envelope: bytes


@dataclass(frozen=True)
class AttemptResult:
    succeeded: bool
    retryable: bool
    status_code: int | None
    error: str | None
    response_body: str | None


class DeliveryService:
    """Claim briefly, send with a renewed lease, then finalize atomically."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.client = client
        self.settings = settings

    async def claim_due(self, limit: int) -> list[ClaimedDelivery]:
        if limit <= 0:
            return []
        claims: list[ClaimedDelivery] = []
        async with self.session_factory() as session:
            async with session.begin():
                now = await database_now(session)
                deliveries = await select_fair_deliveries(
                    session, self.settings, now, limit
                )
                for delivery in deliveries:
                    token = str(uuid4())
                    delivery.status = "processing"
                    delivery.attempt_count += 1
                    delivery.lease_token = token
                    delivery.lease_expires_at = now + timedelta(
                        seconds=self.settings.worker_lease_seconds
                    )
                    delivery.updated_at = now
                    claims.append(
                        ClaimedDelivery(
                            id=delivery.id,
                            public_id=delivery.public_id,
                            organization_id=delivery.organization_id,
                            endpoint_id=delivery.endpoint_id,
                            lease_token=token,
                            attempt_number=delivery.attempt_count,
                            endpoint_public_id=(
                                delivery.endpoint_public_id_snapshot
                            ),
                            endpoint_url=delivery.endpoint_url_snapshot,
                            endpoint_secret_version=(
                                delivery.signing_secret_version_snapshot
                            ),
                            endpoint_active=(
                                delivery.endpoint_active_snapshot
                            ),
                            event_public_id=delivery.event.public_id,
                            event_type=delivery.event.event_type,
                            canonical_envelope=bytes(
                                delivery.event.canonical_envelope
                            ),
                        )
                    )
        return claims

    async def renew_lease(self, claim: ClaimedDelivery) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                now = await database_now(session)
                delivery = await session.scalar(
                    select(Delivery)
                    .where(
                        Delivery.id == claim.id,
                        Delivery.status == "processing",
                        Delivery.lease_token == claim.lease_token,
                        Delivery.lease_expires_at > now,
                    )
                    .with_for_update()
                )
                if delivery is None:
                    return False
                delivery.lease_expires_at = now + timedelta(
                    seconds=self.settings.worker_lease_seconds
                )
                delivery.updated_at = now
        return True

    async def release_claim(self, claim: ClaimedDelivery) -> bool:
        """Make canceled work immediately reclaimable during shutdown."""
        async with self.session_factory() as session:
            async with session.begin():
                now = await database_now(session)
                delivery = await session.scalar(
                    select(Delivery)
                    .where(
                        Delivery.id == claim.id,
                        Delivery.status == "processing",
                        Delivery.lease_token == claim.lease_token,
                    )
                    .with_for_update()
                )
                if delivery is None:
                    return False
                delivery.status = "retry_scheduled"
                delivery.next_attempt_at = now
                delivery.lease_token = None
                delivery.lease_expires_at = None
                delivery.updated_at = now
        return True

    async def _heartbeat(
        self, claim: ClaimedDelivery, stop: asyncio.Event
    ) -> bool:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.settings.worker_heartbeat_seconds,
                )
            except TimeoutError:
                if not await self.renew_lease(claim):
                    return False
        return True

    async def _perform_attempt(self, claim: ClaimedDelivery) -> AttemptResult:
        retryable = False
        status_code: int | None = None
        error: str | None = None
        response_body: str | None = None
        if not claim.endpoint_active:
            error = "endpoint_inactive_at_acceptance"
        else:
            try:
                async with asyncio.timeout(
                    self.settings.worker_attempt_timeout_seconds
                ):
                    await validate_webhook_url(
                        claim.endpoint_url,
                        bool(self.settings.allow_http_webhooks),
                    )
                    secret = endpoint_secret(
                        self.settings.webhook_signing_key,
                        claim.endpoint_public_id,
                        claim.endpoint_secret_version,
                    )
                    timestamp, signature = sign_payload(
                        claim.canonical_envelope, secret
                    )
                    async with self.client.stream(
                        "POST",
                        claim.endpoint_url,
                        content=claim.canonical_envelope,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "webhook-platform/1.0",
                            "Webhook-Id": claim.event_public_id,
                            "Webhook-Timestamp": str(timestamp),
                            "Webhook-Signature": signature,
                            "Webhook-Event": claim.event_type,
                            "Webhook-Attempt": str(claim.attempt_number),
                        },
                    ) as response:
                        status_code = response.status_code
                        remaining = self.settings.webhook_response_max_bytes
                        chunks: list[bytes] = []
                        async for chunk in response.aiter_bytes():
                            if remaining <= 0:
                                break
                            captured = chunk[:remaining]
                            chunks.append(captured)
                            remaining -= len(captured)
                        response_body = b"".join(chunks).decode(
                            "utf-8", errors="replace"
                        )
                retryable = (
                    status_code in {408, 425, 429}
                    or (status_code is not None and status_code >= 500)
                )
                if status_code is not None and not 200 <= status_code < 300:
                    error = (
                        "retryable_http_status"
                        if retryable
                        else "http_status"
                    )
            except TimeoutError:
                retryable = True
                error = "attempt_timeout"
            except httpx.ProxyError:
                error = "egress_proxy_denied"
                record_security_deny(
                    SecurityLayer.PROXY,
                    SecurityDenyReason.PROXY_CONNECT_DENIED,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                retryable = True
                error = type(exc).__name__
            except UnsafeWebhookUrl as exc:
                error = f"unsafe_webhook_url:{exc.reason.value}"
                record_security_deny(SecurityLayer.ATTEMPT, exc.reason)
            except (TypeError, ValueError) as exc:
                error = type(exc).__name__

        return AttemptResult(
            succeeded=(
                status_code is not None and 200 <= status_code < 300
            ),
            retryable=retryable,
            status_code=status_code,
            error=error,
            response_body=response_body,
        )

    async def deliver(self, claim: ClaimedDelivery) -> bool:
        started = utcnow()
        heartbeat_stop = asyncio.Event()
        attempt_task = asyncio.create_task(self._perform_attempt(claim))
        heartbeat_task = asyncio.create_task(
            self._heartbeat(claim, heartbeat_stop)
        )
        try:
            done, _ = await asyncio.wait(
                {attempt_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and not heartbeat_task.result():
                attempt_task.cancel()
                with suppress(asyncio.CancelledError):
                    await attempt_task
                return False
            result = await attempt_task
            heartbeat_stop.set()
            if not await heartbeat_task:
                return False
            return await self._finalize(
                claim=claim,
                started=started,
                succeeded=result.succeeded,
                retryable=result.retryable,
                status_code=result.status_code,
                error=result.error,
                response_body=result.response_body,
            )
        finally:
            heartbeat_stop.set()
            for task in (attempt_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                attempt_task, heartbeat_task, return_exceptions=True
            )

    async def _finalize(
        self,
        claim: ClaimedDelivery,
        started: datetime,
        succeeded: bool,
        retryable: bool,
        status_code: int | None,
        error: str | None,
        response_body: str | None,
    ) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                finished = await database_now(session)
                delivery = await session.scalar(
                    select(Delivery)
                    .where(
                        Delivery.id == claim.id,
                        Delivery.status == "processing",
                        Delivery.lease_token == claim.lease_token,
                        Delivery.lease_expires_at > finished,
                    )
                    .with_for_update()
                )
                if delivery is None:
                    return False
                if succeeded:
                    outcome = "succeeded"
                    delivery.status = "succeeded"
                    delivery.succeeded_at = finished
                    delivery.next_attempt_at = finished
                elif (
                    retryable
                    and claim.attempt_number
                    < self.settings.webhook_max_attempts
                ):
                    outcome = "retry_scheduled"
                    delivery.status = "retry_scheduled"
                    ceiling = min(
                        self.settings.webhook_backoff_cap_seconds,
                        self.settings.webhook_backoff_base_seconds
                        * (2 ** (claim.attempt_number - 1)),
                    )
                    delivery.next_attempt_at = finished + timedelta(
                        seconds=random.uniform(0, ceiling)
                    )
                else:
                    outcome = "dead"
                    delivery.status = "dead"
                    delivery.next_attempt_at = finished
                delivery.last_http_status = status_code
                delivery.last_error = error
                delivery.lease_token = None
                delivery.lease_expires_at = None
                delivery.updated_at = finished
                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery.id,
                        attempt_number=claim.attempt_number,
                        started_at=started,
                        finished_at=finished,
                        outcome=outcome,
                        http_status=status_code,
                        error=error,
                        response_body=response_body,
                    )
                )
        return True
