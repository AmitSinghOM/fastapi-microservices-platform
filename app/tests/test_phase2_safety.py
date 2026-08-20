import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.delivery_service as delivery_module
from app.api_key_usage import ApiKeyUsageTracker
from app.config import Settings
from app.models import (
    ApiKey,
    Delivery,
    DeliveryAttempt,
    Event,
    Project,
    WebhookEndpoint,
)
from app.services.delivery_service import ClaimedDelivery, DeliveryService
from app.tests.test_delivery_contracts import (
    expire_lease,
    finalize_success,
    now,
    seed_delivery,
    worker_settings,
)
from app.worker import run_delivery_loop


def fake_claim(number: int) -> ClaimedDelivery:
    return ClaimedDelivery(
        id=number,
        public_id=str(uuid4()),
        organization_id=number,
        endpoint_id=number,
        lease_token=str(uuid4()),
        attempt_number=1,
        endpoint_public_id=str(uuid4()),
        endpoint_url="https://receiver.example/webhooks",
        endpoint_secret_version=1,
        endpoint_active=True,
        event_public_id=str(uuid4()),
        event_type="phase2.test",
        canonical_envelope=b"{}",
    )


@pytest.mark.asyncio
async def test_expired_unreclaimed_lease_cannot_finalize(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    delivery_id = await seed_delivery(sqlite_session_factory)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            sqlite_session_factory, client, worker_settings()
        )
        claim = (await service.claim_due(1))[0]
        await expire_lease(sqlite_session_factory, delivery_id)
        assert await finalize_success(service, claim) is False

    async with sqlite_session_factory() as session:
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == delivery_id
                )
            )
        )
    assert attempts == []


@pytest.mark.asyncio
async def test_lease_heartbeat_renews_only_live_owner(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    delivery_id = await seed_delivery(sqlite_session_factory)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            sqlite_session_factory, client, worker_settings()
        )
        claim = (await service.claim_due(1))[0]
        async with sqlite_session_factory() as session:
            stored = await session.get(Delivery, delivery_id)
            assert stored is not None
            before = stored.lease_expires_at
        await asyncio.sleep(0.01)
        assert await service.renew_lease(claim) is True
        async with sqlite_session_factory() as session:
            stored = await session.get(Delivery, delivery_id)
            assert stored is not None
            after = stored.lease_expires_at
        assert before is not None and after is not None and after > before
        await expire_lease(sqlite_session_factory, delivery_id)
        assert await service.renew_lease(claim) is False


@pytest.mark.asyncio
async def test_claim_uses_immutable_acceptance_snapshots(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    del db_session
    delivery_id = await seed_delivery(sqlite_session_factory)
    async with sqlite_session_factory() as session:
        async with session.begin():
            delivery = await session.get(Delivery, delivery_id)
            assert delivery is not None
            endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id)
            assert endpoint is not None
            endpoint.url = "https://changed.example/new"
            endpoint.is_active = False
            endpoint.secret_version = 2
            event = await session.get(Event, delivery.event_id)
            assert event is not None
            event.payload = {"value": "mutated"}

    async def allow_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    captured: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(
        delivery_module, "validate_webhook_url", allow_target
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(
            sqlite_session_factory, client, worker_settings()
        )
        claim = (await service.claim_due(1))[0]
        assert claim.endpoint_url == "https://receiver.example/webhooks"
        assert claim.endpoint_active is True
        assert claim.endpoint_secret_version == 1
        assert b'"value":1' in claim.canonical_envelope
        assert b"mutated" not in claim.canonical_envelope
        assert await service.deliver(claim) is True

    assert captured[0].content == claim.canonical_envelope


@pytest.mark.asyncio
async def test_worker_claims_no_more_than_free_slots():
    settings = worker_settings().model_copy(
        update={"worker_concurrency": 2, "worker_batch_size": 50}
    )
    stop = asyncio.Event()

    class FakeService:
        def __init__(self) -> None:
            self.limits: list[int] = []
            self.claimed = False

        async def claim_due(self, limit: int) -> list[ClaimedDelivery]:
            self.limits.append(limit)
            if self.claimed:
                return []
            self.claimed = True
            return [fake_claim(1), fake_claim(2)]

        async def deliver(self, claim: ClaimedDelivery) -> bool:
            del claim
            stop.set()
            await asyncio.sleep(0)
            return True

        async def release_claim(self, claim: ClaimedDelivery) -> bool:
            del claim
            return True

    service = FakeService()
    await run_delivery_loop(service, settings, stop)  # type: ignore[arg-type]
    assert service.limits == [2]


@pytest.mark.asyncio
async def test_shutdown_releases_work_that_exceeds_grace():
    settings = worker_settings().model_copy(
        update={
            "worker_concurrency": 1,
            "worker_batch_size": 10,
            "worker_shutdown_grace_seconds": 0.05,
        }
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    released: list[int] = []

    class SlowService:
        def __init__(self) -> None:
            self.claimed = False

        async def claim_due(self, limit: int) -> list[ClaimedDelivery]:
            assert limit == 1
            if self.claimed:
                return []
            self.claimed = True
            return [fake_claim(1)]

        async def deliver(self, claim: ClaimedDelivery) -> bool:
            del claim
            started.set()
            await asyncio.Event().wait()
            return True

        async def release_claim(self, claim: ClaimedDelivery) -> bool:
            released.append(claim.id)
            return True

    service = SlowService()
    task = asyncio.create_task(
        run_delivery_loop(service, settings, stop)  # type: ignore[arg-type]
    )
    await started.wait()
    stop.set()
    await task
    assert released == [1]


@pytest.mark.asyncio
async def test_api_key_usage_is_coalesced_and_flushed_on_shutdown(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    await seed_delivery(sqlite_session_factory)
    async with sqlite_session_factory() as session:
        async with session.begin():
            project_id = await session.scalar(select(Project.id))
            assert project_id is not None
            api_key = ApiKey(
                public_id=str(uuid4()),
                project_id=project_id,
                name="usage tracker",
                key_prefix=uuid4().hex[:12],
                key_digest="d" * 64,
                is_active=True,
                created_at=now(),
            )
            session.add(api_key)
            await session.flush()
            api_key_id = api_key.id

    tracker = ApiKeyUsageTracker(
        sqlite_session_factory,
        worker_settings().model_copy(
            update={"api_key_usage_flush_seconds": 60.0}
        ),
    )
    first = now()
    latest = first + timedelta(seconds=1)
    await tracker.start()
    await tracker.record(api_key_id, first)
    await tracker.record(api_key_id, latest)
    await tracker.stop()
    newest = latest + timedelta(seconds=1)
    await tracker.start()
    await tracker.record(api_key_id, newest)
    await tracker.stop()

    async with sqlite_session_factory() as session:
        api_key = await session.get(ApiKey, api_key_id)
    assert api_key is not None and api_key.last_used_at is not None
    assert api_key.last_used_at.replace(tzinfo=newest.tzinfo) == newest


def test_lease_configuration_enforces_complete_safety_window():
    with pytest.raises(ValueError, match="WORKER_LEASE_SECONDS"):
        Settings(
            _env_file=None,
            environment="test",
            secret_key="s" * 32,
            api_key_pepper="p" * 32,
            webhook_signing_key="w" * 32,
            worker_lease_seconds=20,
            worker_attempt_timeout_seconds=15,
            worker_heartbeat_seconds=3,
            worker_finalization_margin_seconds=2,
            worker_shutdown_grace_seconds=20,
        )


@pytest.mark.asyncio
async def test_long_request_renews_lease_while_in_flight(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    del db_session
    await seed_delivery(sqlite_session_factory)

    async def allow_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    async def receiver(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(0.08)
        return httpx.Response(200)

    monkeypatch.setattr(
        delivery_module, "validate_webhook_url", allow_target
    )
    settings = worker_settings().model_copy(
        update={
            "worker_heartbeat_seconds": 0.02,
            "worker_attempt_timeout_seconds": 0.5,
        }
    )

    class CountingService(DeliveryService):
        renewals = 0

        async def renew_lease(self, claim: ClaimedDelivery) -> bool:
            self.renewals += 1
            return await super().renew_lease(claim)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = CountingService(
            sqlite_session_factory, client, settings
        )
        claim = (await service.claim_due(1))[0]
        assert await service.deliver(claim) is True
        assert service.renewals >= 2
