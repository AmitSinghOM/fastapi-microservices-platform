import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admission import endpoint_quota_values
from app.exceptions import QuotaExceededError
from app.models import (
    Delivery,
    DeliveryAttempt,
    EndpointQuotaState,
    Event,
    GlobalControlState,
    Organization,
    Project,
    WebhookEndpoint,
)
from app.services.delivery_service import DeliveryService
from app.services.webhook_service import WebhookService
from app.tests.test_delivery_contracts import (
    expire_lease,
    finalize_success,
    seed_delivery,
    worker_settings,
)

pytestmark = pytest.mark.postgres


async def seed_project(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    created_at = datetime.now(timezone.utc)
    async with factory() as session:
        async with session.begin():
            organization = Organization(
                public_id=str(uuid4()),
                name="Concurrent ingestion",
                created_at=created_at,
            )
            session.add(organization)
            await session.flush()
            project = Project(
                public_id=str(uuid4()),
                organization_id=organization.id,
                name="PostgreSQL contracts",
                is_active=True,
                created_at=created_at,
            )
            session.add(project)
            await session.flush()
            endpoint = WebhookEndpoint(
                public_id=str(uuid4()),
                project_id=project.id,
                url="https://receiver.example/webhooks",
                is_active=True,
                secret_version=1,
                created_at=created_at,
                updated_at=created_at,
            )
            session.add(endpoint)
            return project.id


@pytest.mark.asyncio
async def test_concurrent_idempotency_creates_one_event_and_fanout(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    project_id = await seed_project(postgres_session_factory)

    async def ingest() -> str:
        async with postgres_session_factory() as session:
            project = await session.get(Project, project_id)
            assert project is not None
            event = await WebhookService(
                session,
                worker_settings(),
            ).ingest_event(
                project,
                "concurrent-order-0001",
                "order.created",
                {"order_id": "0001"},
            )
            return event.public_id

    event_ids = await asyncio.gather(ingest(), ingest())

    assert event_ids[0] == event_ids[1]
    async with postgres_session_factory() as session:
        event_count = await session.scalar(select(func.count(Event.id)))
        delivery_count = await session.scalar(select(func.count(Delivery.id)))
    assert event_count == 1
    assert delivery_count == 1


@pytest.mark.asyncio
async def test_competing_workers_claim_delivery_once(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    await seed_delivery(postgres_session_factory)
    async with httpx.AsyncClient() as client:
        first_worker = DeliveryService(
            postgres_session_factory,
            client,
            worker_settings(),
        )
        second_worker = DeliveryService(
            postgres_session_factory,
            client,
            worker_settings(),
        )
        first_claims, second_claims = await asyncio.gather(
            first_worker.claim_due(1),
            second_worker.claim_due(1),
        )

    claims = first_claims + second_claims
    assert len(claims) == 1
    assert len({claim.public_id for claim in claims}) == 1

    async with postgres_session_factory() as session:
        delivery = await session.scalar(select(Delivery))
    assert delivery is not None
    assert delivery.status == "processing"
    assert delivery.attempt_count == 1
    assert delivery.lease_token == claims[0].lease_token


@pytest.mark.asyncio
async def test_heartbeat_prevents_competing_reclaim(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    await seed_delivery(postgres_session_factory)
    async with httpx.AsyncClient() as client:
        owner = DeliveryService(
            postgres_session_factory, client, worker_settings()
        )
        competitor = DeliveryService(
            postgres_session_factory, client, worker_settings()
        )
        claim = (await owner.claim_due(1))[0]
        assert await owner.renew_lease(claim) is True
        assert await competitor.claim_due(1) == []


@pytest.mark.asyncio
async def test_concurrent_finalization_inserts_one_attempt(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    delivery_id = await seed_delivery(postgres_session_factory)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            postgres_session_factory, client, worker_settings()
        )
        claim = (await service.claim_due(1))[0]
        results = await asyncio.gather(
            finalize_success(service, claim),
            finalize_success(service, claim),
        )

    assert sorted(results) == [False, True]
    async with postgres_session_factory() as session:
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == delivery_id
                )
            )
        )
    assert [attempt.attempt_number for attempt in attempts] == [1]


@pytest.mark.asyncio
async def test_expired_owner_cannot_finalize_before_reclaim(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    delivery_id = await seed_delivery(postgres_session_factory)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            postgres_session_factory, client, worker_settings()
        )
        claim = (await service.claim_due(1))[0]
        await expire_lease(postgres_session_factory, delivery_id)
        assert await finalize_success(service, claim) is False


async def seed_tenant_queue(
    factory: async_sessionmaker[AsyncSession],
    name: str,
    endpoint_delivery_counts: list[int],
    due_age_seconds: float = 60.0,
) -> tuple[int, list[int], list[str]]:
    now = datetime.now(timezone.utc)
    due_at = now - timedelta(seconds=due_age_seconds)
    delivery_ids: list[str] = []
    async with factory() as session:
        async with session.begin():
            organization = Organization(
                public_id=str(uuid4()), name=name, created_at=now
            )
            session.add(organization)
            await session.flush()
            project = Project(
                public_id=str(uuid4()),
                organization_id=organization.id,
                name=name,
                is_active=True,
                created_at=now,
            )
            session.add(project)
            await session.flush()
            event = Event(
                public_id=str(uuid4()),
                project_id=project.id,
                idempotency_key=str(uuid4()),
                event_type="fairness.test",
                payload={},
                payload_hash="f" * 64,
                canonical_envelope=b"{}",
                created_at=now,
            )
            session.add(event)
            await session.flush()
            endpoint_ids: list[int] = []
            for index, count in enumerate(endpoint_delivery_counts):
                endpoint = WebhookEndpoint(
                    public_id=str(uuid4()),
                    project_id=project.id,
                    url=f"https://receiver.example/{name}/{index}",
                    is_active=True,
                    secret_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(endpoint)
                await session.flush()
                endpoint_ids.append(endpoint.id)
                for _ in range(count):
                    public_id = str(uuid4())
                    delivery_ids.append(public_id)
                    session.add(
                        Delivery(
                            public_id=public_id,
                            organization_id=organization.id,
                            event_id=event.id,
                            endpoint_id=endpoint.id,
                            endpoint_public_id_snapshot=endpoint.public_id,
                            endpoint_url_snapshot=endpoint.url,
                            endpoint_active_snapshot=True,
                            signing_secret_version_snapshot=1,
                            status="pending",
                            attempt_count=0,
                            next_attempt_at=due_at,
                            created_at=now,
                            updated_at=now,
                        )
                    )
    return organization.id, endpoint_ids, delivery_ids


async def mark_succeeded(
    factory: async_sessionmaker[AsyncSession], public_id: str
) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(Delivery)
                .where(Delivery.public_id == public_id)
                .values(status="succeeded")
            )


@pytest.mark.asyncio
async def test_concurrent_distinct_events_share_tenant_burst(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    project_id = await seed_project(postgres_session_factory)
    settings = worker_settings().model_copy(
        update={
            "tenant_event_burst": 1,
            "tenant_event_rate_per_second": 0.001,
        }
    )

    async def ingest(idempotency_key: str):
        async with postgres_session_factory() as session:
            project = await session.get(Project, project_id)
            assert project is not None
            return await WebhookService(session, settings).ingest_event(
                project,
                idempotency_key,
                "quota.test",
                {"key": idempotency_key},
            )

    results = await asyncio.gather(
        ingest("quota-event-0001"),
        ingest("quota-event-0002"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Event) for result in results) == 1
    assert sum(
        isinstance(result, QuotaExceededError) for result in results
    ) == 1
    async with postgres_session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 1
        assert await session.scalar(select(func.count(Delivery.id))) == 1


@pytest.mark.asyncio
async def test_competing_workers_share_global_tenant_and_endpoint_caps(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    first_organization, first_endpoints, _ = await seed_tenant_queue(
        postgres_session_factory, "capacity-a", [3, 3]
    )
    second_organization, second_endpoints, _ = await seed_tenant_queue(
        postgres_session_factory, "capacity-b", [3]
    )
    settings = worker_settings().model_copy(
        update={
            "worker_global_concurrency": 3,
            "tenant_in_flight_deliveries": 2,
            "endpoint_concurrency": 1,
            "endpoint_rate_burst": 100,
        }
    )
    async with httpx.AsyncClient() as client:
        workers = [
            DeliveryService(postgres_session_factory, client, settings)
            for _ in range(2)
        ]
        worker_claims = await asyncio.gather(
            *(worker.claim_due(3) for worker in workers)
        )

    claims = [claim for batch in worker_claims for claim in batch]
    assert len(claims) == 3
    organization_counts = Counter(
        claim.organization_id for claim in claims
    )
    endpoint_counts = Counter(claim.endpoint_id for claim in claims)
    assert set(organization_counts) == {
        first_organization,
        second_organization,
    }
    assert max(organization_counts.values()) <= 2
    assert max(endpoint_counts.values()) <= 1
    assert set(endpoint_counts) <= set(first_endpoints + second_endpoints)


@pytest.mark.asyncio
async def test_noisy_tenant_keeps_healthy_age_within_twice_baseline(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    settings = worker_settings().model_copy(
        update={
            "worker_global_concurrency": 1,
            "tenant_in_flight_deliveries": 1,
            "endpoint_concurrency": 1,
            "endpoint_rate_burst": 100,
        }
    )
    _, _, baseline_ids = await seed_tenant_queue(
        postgres_session_factory, "baseline", [1]
    )
    async with postgres_session_factory() as session:
        baseline_due = await session.scalar(
            select(Delivery.next_attempt_at).where(
                Delivery.public_id == baseline_ids[0]
            )
        )
    assert baseline_due is not None

    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            postgres_session_factory, client, settings
        )
        baseline_claim = (await service.claim_due(1))[0]
        baseline_age = (
            datetime.now(timezone.utc) - baseline_due
        ).total_seconds()
        await mark_succeeded(
            postgres_session_factory, baseline_claim.public_id
        )

        async with postgres_session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(GlobalControlState)
                    .where(GlobalControlState.id == 1)
                    .values(tenant_cursor_organization_id=None)
                )

        await seed_tenant_queue(
            postgres_session_factory,
            "excessive-traffic",
            [50],
            due_age_seconds=600.0,
        )
        _, _, healthy_ids = await seed_tenant_queue(
            postgres_session_factory,
            "healthy",
            [1],
        )
        async with postgres_session_factory() as session:
            healthy_due = await session.scalar(
                select(Delivery.next_attempt_at).where(
                    Delivery.public_id == healthy_ids[0]
                )
            )
        assert healthy_due is not None

        healthy_position = None
        healthy_age = None
        for position in range(1, 3):
            claim = (await service.claim_due(1))[0]
            if claim.public_id == healthy_ids[0]:
                healthy_position = position
                healthy_age = (
                    datetime.now(timezone.utc) - healthy_due
                ).total_seconds()
                break
            await mark_succeeded(
                postgres_session_factory, claim.public_id
            )

    assert healthy_position is not None
    assert healthy_position <= 2
    assert healthy_age is not None
    assert healthy_age <= baseline_age * 2


@pytest.mark.asyncio
async def test_competing_workers_allow_one_half_open_probe(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    _, endpoint_ids, _ = await seed_tenant_queue(
        postgres_session_factory, "half-open", [2]
    )
    endpoint_id = endpoint_ids[0]
    settings = worker_settings().model_copy(
        update={
            "worker_global_concurrency": 2,
            "tenant_in_flight_deliveries": 2,
            "endpoint_concurrency": 2,
            "endpoint_retry_burst": 10,
        }
    )
    now = datetime.now(timezone.utc)
    async with postgres_session_factory() as session:
        async with session.begin():
            state = EndpointQuotaState(
                **endpoint_quota_values(endpoint_id, settings, now)
            )
            state.circuit_state = "open"
            state.circuit_open_until = now - timedelta(seconds=1)
            session.add(state)

    async with httpx.AsyncClient() as client:
        workers = [
            DeliveryService(postgres_session_factory, client, settings)
            for _ in range(2)
        ]
        batches = await asyncio.gather(
            *(worker.claim_due(2) for worker in workers)
        )
    claims = [claim for batch in batches for claim in batch]
    assert len(claims) == 1
    async with postgres_session_factory() as session:
        state = await session.get(EndpointQuotaState, endpoint_id)
        assert state is not None
        assert state.circuit_state == "half_open"
        assert state.half_open_probe_delivery_id == claims[0].id


@pytest.mark.asyncio
async def test_outage_retries_share_endpoint_retry_budget(
    postgres_session_factory: async_sessionmaker[AsyncSession],
):
    _, endpoint_ids, _ = await seed_tenant_queue(
        postgres_session_factory, "retry-budget", [10]
    )
    endpoint_id = endpoint_ids[0]
    settings = worker_settings().model_copy(
        update={
            "worker_global_concurrency": 10,
            "tenant_in_flight_deliveries": 10,
            "endpoint_concurrency": 10,
            "endpoint_retry_burst": 2,
            "endpoint_retry_rate_per_second": 0.001,
        }
    )
    now = datetime.now(timezone.utc)
    async with postgres_session_factory() as session:
        async with session.begin():
            deliveries = list(await session.scalars(select(Delivery)))
            for delivery in deliveries:
                delivery.status = "retry_scheduled"
                delivery.attempt_count = 1
            session.add(
                EndpointQuotaState(
                    **endpoint_quota_values(endpoint_id, settings, now)
                )
            )

    async with httpx.AsyncClient() as client:
        workers = [
            DeliveryService(postgres_session_factory, client, settings)
            for _ in range(2)
        ]
        batches = await asyncio.gather(
            *(worker.claim_due(10) for worker in workers)
        )
    claims = [claim for batch in batches for claim in batch]
    assert len(claims) == 2
    assert {claim.endpoint_id for claim in claims} == {endpoint_id}