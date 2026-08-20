from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import Settings
from app.models import Delivery, DeliveryAttempt, EndpointQuotaState
from app.observability import (
    delivery_span,
    record_circuit_transition,
    record_claim,
    record_finalization,
    record_lease,
    record_stale_finalization,
    observe_pool_acquisition,
)
from app.retry_policy import (
    aware,
    is_retryable_status,
    jittered_backoff,
    parse_retry_after,
)
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
    traceparent: str | None = None
    tracestate: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class AttemptResult:
    succeeded: bool
    retryable: bool
    status_code: int | None
    error: str | None
    response_body: str | None
    retry_after_seconds: float | None = None


class DeliveryService:
    """Claim briefly, send with a renewed lease, then finalize atomically."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        settings: Settings,
        engine: AsyncEngine | None = None,
    ):
        self.session_factory = session_factory
        self.client = client
        self.settings = settings
        self.engine = engine

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        if self.engine is None:
            async with self.session_factory() as session:
                yield session
            return
        started = asyncio.get_running_loop().time()
        acquired = False
        try:
            async with self.engine.connect() as connection:
                acquired = True
                observe_pool_acquisition(
                    asyncio.get_running_loop().time() - started
                )
                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    yield session
        except Exception:
            if not acquired:
                observe_pool_acquisition(
                    asyncio.get_running_loop().time() - started,
                    "error",
                )
            raise

    async def claim_due(self, limit: int) -> list[ClaimedDelivery]:
        if limit <= 0:
            return []
        claims: list[ClaimedDelivery] = []
        reclaimed = 0
        async with self._session() as session:
            async with session.begin():
                now = await database_now(session)
                deliveries = await select_fair_deliveries(
                    session, self.settings, now, limit
                )
                for delivery in deliveries:
                    if delivery.status == "processing":
                        reclaimed += 1
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
                            traceparent=delivery.event.traceparent,
                            tracestate=delivery.event.tracestate,
                            created_at=delivery.created_at,
                        )
                    )
        record_claim(len(claims), reclaimed)
        return claims

    async def renew_lease(self, claim: ClaimedDelivery) -> bool:
        async with self._session() as session:
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
                    record_lease("lost")
                    return False
                delivery.lease_expires_at = now + timedelta(
                    seconds=self.settings.worker_lease_seconds
                )
                delivery.updated_at = now
        record_lease("renewed")
        return True

    async def release_claim(self, claim: ClaimedDelivery) -> bool:
        """Make canceled work immediately reclaimable during shutdown."""
        async with self._session() as session:
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
                    record_lease("release_stale")
                    return False
                delivery.status = "retry_scheduled"
                delivery.next_attempt_at = now
                delivery.lease_token = None
                delivery.lease_expires_at = None
                delivery.updated_at = now
        record_lease("released")
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
        retry_after_seconds: float | None = None
        age = (utcnow() - aware(claim.created_at)).total_seconds()
        if age >= self.settings.webhook_max_delivery_age_seconds:
            error = "max_delivery_age"
        elif not claim.endpoint_active:
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
                        retry_after_seconds = parse_retry_after(
                            response.headers.get("Retry-After"),
                            utcnow(),
                            self.settings.webhook_retry_after_max_seconds,
                        )
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
                retryable = is_retryable_status(status_code)
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
            retry_after_seconds=retry_after_seconds,
        )

    async def deliver(self, claim: ClaimedDelivery) -> bool:
        with delivery_span(claim) as span:
            finalized = await self._deliver_with_heartbeat(claim)
            span.set_attribute("webhook.finalized", finalized)
            return finalized

    async def _deliver_with_heartbeat(
        self, claim: ClaimedDelivery
    ) -> bool:
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
                retry_after_seconds=result.retry_after_seconds,
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
        retry_after_seconds: float | None = None,
    ) -> bool:
        async with self._session() as session:
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
                    record_stale_finalization()
                    return False
                endpoint_state = await session.scalar(
                    select(EndpointQuotaState)
                    .where(
                        EndpointQuotaState.endpoint_id
                        == delivery.endpoint_id
                    )
                    .with_for_update()
                )
                if endpoint_state is None:
                    raise RuntimeError("Endpoint retry state is unavailable")

                previous_circuit_state = endpoint_state.circuit_state
                is_probe = (
                    endpoint_state.half_open_probe_delivery_id == delivery.id
                )
                if succeeded:
                    endpoint_state.retry_tokens = min(
                        float(self.settings.endpoint_retry_burst),
                        float(endpoint_state.retry_tokens)
                        + self.settings.endpoint_retry_success_refill,
                    )
                    endpoint_state.retry_refilled_at = finished
                    endpoint_state.circuit_state = "closed"
                    endpoint_state.consecutive_failures = 0
                    endpoint_state.circuit_open_until = None
                    endpoint_state.half_open_probe_delivery_id = None
                elif retryable:
                    endpoint_state.consecutive_failures += 1
                    if is_probe or (
                        endpoint_state.circuit_state == "closed"
                        and endpoint_state.consecutive_failures
                        >= self.settings.endpoint_circuit_failure_threshold
                    ):
                        endpoint_state.circuit_state = "open"
                        endpoint_state.circuit_open_until = (
                            finished
                            + timedelta(
                                seconds=(
                                    self.settings.endpoint_circuit_open_seconds
                                )
                            )
                        )
                        endpoint_state.half_open_probe_delivery_id = None
                elif is_probe and error == "max_delivery_age":
                    endpoint_state.circuit_state = "open"
                    endpoint_state.circuit_open_until = finished
                    endpoint_state.half_open_probe_delivery_id = None
                elif is_probe or error != "max_delivery_age":
                    endpoint_state.circuit_state = "closed"
                    endpoint_state.consecutive_failures = 0
                    endpoint_state.circuit_open_until = None
                    endpoint_state.half_open_probe_delivery_id = None
                endpoint_state.updated_at = finished
                circuit_transition = (
                    endpoint_state.circuit_state
                    if endpoint_state.circuit_state != previous_circuit_state
                    else None
                )

                delivery_deadline = aware(delivery.created_at) + timedelta(
                    seconds=self.settings.webhook_max_delivery_age_seconds
                )
                age_expired = finished >= delivery_deadline
                if succeeded:
                    outcome = "succeeded"
                    delivery.status = "succeeded"
                    delivery.succeeded_at = finished
                    delivery.next_attempt_at = finished
                    delivery.dead_at = None
                    delivery.dead_reason = None
                elif (
                    retryable
                    and claim.attempt_number
                    < self.settings.webhook_max_attempts
                    and not age_expired
                ):
                    delay = max(
                        jittered_backoff(
                            claim.attempt_number,
                            self.settings.webhook_backoff_base_seconds,
                            self.settings.webhook_backoff_cap_seconds,
                        ),
                        retry_after_seconds or 0.0,
                    )
                    next_attempt_at = finished + timedelta(seconds=delay)
                    if next_attempt_at < delivery_deadline:
                        outcome = "retry_scheduled"
                        delivery.status = "retry_scheduled"
                        delivery.next_attempt_at = next_attempt_at
                        delivery.dead_at = None
                        delivery.dead_reason = None
                    else:
                        outcome = "dead"
                        delivery.status = "dead"
                        delivery.next_attempt_at = finished
                        delivery.dead_at = finished
                        delivery.dead_reason = "max_delivery_age"
                else:
                    outcome = "dead"
                    delivery.status = "dead"
                    delivery.next_attempt_at = finished
                    delivery.dead_at = finished
                    if age_expired or error == "max_delivery_age":
                        dead_reason = "max_delivery_age"
                    elif (
                        retryable
                        and claim.attempt_number
                        >= self.settings.webhook_max_attempts
                    ):
                        dead_reason = "max_attempts"
                    elif status_code is not None and 400 <= status_code < 500:
                        dead_reason = "non_retryable_http"
                    else:
                        dead_reason = error or "non_retryable_failure"
                    delivery.dead_reason = dead_reason[:64]
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
                attempt_seconds = (
                    aware(finished) - aware(started)
                ).total_seconds()
                end_to_end_seconds = (
                    aware(finished) - aware(delivery.created_at)
                ).total_seconds()
        if circuit_transition is not None:
            record_circuit_transition(circuit_transition)
        record_finalization(
            outcome,
            attempt_seconds,
            end_to_end_seconds,
            claim.attempt_number,
            status_code,
            error,
        )
        return True
