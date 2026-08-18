from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from app.config import Settings
from app.models import Delivery, DeliveryAttempt
from app.webhook_security import (
    UnsafeWebhookUrl,
    canonical_json,
    endpoint_secret,
    sign_payload,
    validate_webhook_url,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimedDelivery:
    id: int
    public_id: str
    lease_token: str
    attempt_number: int
    endpoint_public_id: str
    endpoint_url: str
    endpoint_secret_version: int
    endpoint_active: bool
    event_public_id: str
    event_type: str
    event_payload: object
    event_created_at: datetime


class DeliveryService:
    """Claims briefly, performs HTTP without a transaction, then finalizes."""

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
        now = utcnow()
        claims: list[ClaimedDelivery] = []
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.scalars(
                    select(Delivery)
                    .options(
                        joinedload(Delivery.event),
                        joinedload(Delivery.endpoint),
                    )
                    .where(
                        or_(
                            and_(
                                Delivery.status.in_(
                                    ("pending", "retry_scheduled")
                                ),
                                Delivery.next_attempt_at <= now,
                            ),
                            and_(
                                Delivery.status == "processing",
                                Delivery.lease_expires_at <= now,
                            ),
                        )
                    )
                    .order_by(Delivery.next_attempt_at, Delivery.id)
                    .limit(limit)
                    .with_for_update(of=Delivery, skip_locked=True)
                )
                for delivery in result:
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
                            lease_token=token,
                            attempt_number=delivery.attempt_count,
                            endpoint_public_id=delivery.endpoint.public_id,
                            endpoint_url=delivery.endpoint.url,
                            endpoint_secret_version=(
                                delivery.endpoint.secret_version
                            ),
                            endpoint_active=delivery.endpoint.is_active,
                            event_public_id=delivery.event.public_id,
                            event_type=delivery.event.event_type,
                            event_payload=delivery.event.payload,
                            event_created_at=delivery.event.created_at,
                        )
                    )
        return claims

    async def deliver(self, claim: ClaimedDelivery) -> bool:
        started = utcnow()
        retryable = False
        status_code: int | None = None
        error: str | None = None
        response_body: str | None = None
        if not claim.endpoint_active:
            error = "endpoint_inactive"
        else:
            try:
                await validate_webhook_url(
                    claim.endpoint_url,
                    bool(self.settings.allow_http_webhooks),
                )
                envelope = {
                    "id": claim.event_public_id,
                    "type": claim.event_type,
                    "created_at": claim.event_created_at.isoformat(),
                    "data": claim.event_payload,
                }
                body = canonical_json(envelope)
                secret = endpoint_secret(
                    self.settings.webhook_signing_key,
                    claim.endpoint_public_id,
                    claim.endpoint_secret_version,
                )
                timestamp, signature = sign_payload(body, secret)
                async with self.client.stream(
                    "POST",
                    claim.endpoint_url,
                    content=body,
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
                    status_code in {408, 425, 429} or status_code >= 500
                )
                if not 200 <= status_code < 300:
                    error = (
                        "retryable_http_status"
                        if retryable
                        else "http_status"
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                retryable = True
                error = type(exc).__name__
            except (UnsafeWebhookUrl, TypeError, ValueError) as exc:
                error = type(exc).__name__

        succeeded = status_code is not None and 200 <= status_code < 300
        return await self._finalize(
            claim=claim,
            started=started,
            succeeded=succeeded,
            retryable=retryable,
            status_code=status_code,
            error=error,
            response_body=response_body,
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
        finished = utcnow()
        async with self.session_factory() as session:
            async with session.begin():
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
