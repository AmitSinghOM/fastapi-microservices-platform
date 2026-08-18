from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.delivery_service as delivery_module
from app.config import Settings
from app.models import (
    Delivery,
    DeliveryAttempt,
    Event,
    Organization,
    Project,
    WebhookEndpoint,
)
from app.services.delivery_service import ClaimedDelivery, DeliveryService


def now() -> datetime:
    return datetime.now(timezone.utc)


def worker_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        secret_key="s" * 32,
        api_key_pepper="p" * 32,
        webhook_signing_key="w" * 32,
        allow_http_webhooks=False,
        worker_lease_seconds=5,
    )


async def seed_delivery(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    created_at = now()
    async with factory() as session:
        async with session.begin():
            organization = Organization(
                public_id=str(uuid4()),
                name="Phase 0",
                created_at=created_at,
            )
            session.add(organization)
            await session.flush()
            project = Project(
                public_id=str(uuid4()),
                organization_id=organization.id,
                name="Worker contracts",
                is_active=True,
                created_at=created_at,
            )
            session.add(project)
            await session.flush()
            endpoint = WebhookEndpoint(
                public_id=str(uuid4()),
                project_id=project.id,
                url="https://receiver.example/webhooks",
                description=None,
                is_active=True,
                secret_version=1,
                created_at=created_at,
                updated_at=created_at,
            )
            event = Event(
                public_id=str(uuid4()),
                project_id=project.id,
                idempotency_key=str(uuid4()),
                event_type="phase0.test",
                payload={"value": 1},
                payload_hash="a" * 64,
                created_at=created_at,
            )
            session.add_all((endpoint, event))
            await session.flush()
            delivery = Delivery(
                public_id=str(uuid4()),
                event_id=event.id,
                endpoint_id=endpoint.id,
                status="pending",
                attempt_count=0,
                next_attempt_at=created_at,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(delivery)
            await session.flush()
            return delivery.id


async def expire_lease(
    factory: async_sessionmaker[AsyncSession],
    delivery_id: int,
) -> None:
    async with factory() as session:
        async with session.begin():
            delivery = await session.get(Delivery, delivery_id)
            assert delivery is not None
            delivery.lease_expires_at = now() - timedelta(seconds=1)


async def finalize_success(
    service: DeliveryService,
    claim: ClaimedDelivery,
) -> bool:
    return await service._finalize(
        claim=claim,
        started=now(),
        succeeded=True,
        retryable=False,
        status_code=200,
        error=None,
        response_body="ok",
    )


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_stale_worker_is_rejected(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    delivery_id = await seed_delivery(sqlite_session_factory)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            sqlite_session_factory,
            client,
            worker_settings(),
        )
        original = (await service.claim_due(1))[0]
        await expire_lease(sqlite_session_factory, delivery_id)
        reclaimed = (await service.claim_due(1))[0]

        assert reclaimed.attempt_number == original.attempt_number + 1
        assert reclaimed.lease_token != original.lease_token
        assert await finalize_success(service, original) is False
        assert await finalize_success(service, reclaimed) is True

    async with sqlite_session_factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == delivery_id
                )
            )
        )
    assert delivery is not None and delivery.status == "succeeded"
    assert [attempt.attempt_number for attempt in attempts] == [2]


@pytest.mark.asyncio
async def test_crash_after_http_can_redeliver_same_event(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    del db_session
    delivery_id = await seed_delivery(sqlite_session_factory)
    calls: list[str] = []

    async def allow_test_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    def receiver(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["Webhook-Id"])
        return httpx.Response(200, text="accepted")

    monkeypatch.setattr(
        delivery_module,
        "validate_webhook_url",
        allow_test_target,
    )

    class CrashAfterHttpService(DeliveryService):
        async def _finalize(self, *args, **kwargs) -> bool:
            del args, kwargs
            raise RuntimeError("simulated crash before finalization")

    transport = httpx.MockTransport(receiver)
    async with httpx.AsyncClient(transport=transport) as client:
        crashing = CrashAfterHttpService(
            sqlite_session_factory,
            client,
            worker_settings(),
        )
        first_claim = (await crashing.claim_due(1))[0]
        with pytest.raises(RuntimeError, match="simulated crash"):
            await crashing.deliver(first_claim)

        await expire_lease(sqlite_session_factory, delivery_id)
        healthy = DeliveryService(
            sqlite_session_factory,
            client,
            worker_settings(),
        )
        second_claim = (await healthy.claim_due(1))[0]
        assert await healthy.deliver(second_claim) is True

    assert calls == [first_claim.event_public_id, first_claim.event_public_id]
    async with sqlite_session_factory() as session:
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == delivery_id
                )
            )
        )
    assert [attempt.attempt_number for attempt in attempts] == [2]
