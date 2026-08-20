from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api_key_auth as api_key_auth_module
import app.services.factory as factory_module
import app.services.webhook_service as webhook_module
from app.admission import AdmissionController
from app.config import Settings
from app.exceptions import SaturationError
from app.models import Delivery, Event, Organization, Project, WebhookEndpoint
from app.services.delivery_service import DeliveryService
from app.tests.test_delivery_contracts import worker_settings


@pytest.fixture(autouse=True)
def align_api_key_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        api_key_auth_module,
        "get_settings",
        lambda: factory_module.get_settings(),
    )


def phase4_settings(**updates) -> Settings:
    return worker_settings().model_copy(update=updates)


async def api_project_key(client: httpx.AsyncClient, headers: dict[str, str]):
    organization = await client.post(
        "/v1/organizations", headers=headers, json={"name": str(uuid4())}
    )
    assert organization.status_code == 201, organization.text
    project = await client.post(
        f"/v1/organizations/{organization.json()['public_id']}/projects",
        headers=headers,
        json={"name": str(uuid4())},
    )
    assert project.status_code == 201, project.text
    key = await client.post(
        f"/v1/projects/{project.json()['public_id']}/api-keys",
        headers=headers,
        json={"name": "producer"},
    )
    assert key.status_code == 201, key.text
    return project.json()["public_id"], key.json()["plaintext_key"]


async def allow_target(url: str, allow_http: bool = False) -> str:
    del allow_http
    return url


async def create_api_endpoint(
    client: httpx.AsyncClient,
    bearer: dict[str, str],
    project_id: str,
    suffix: str,
):
    return await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=bearer,
        json={"url": f"https://receiver.example/{suffix}"},
    )


@pytest.mark.asyncio
async def test_event_quota_returns_bounded_429_without_double_charging(
    client: httpx.AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = phase4_settings(
        tenant_event_burst=1,
        tenant_event_rate_per_second=0.001,
        quota_retry_after_max_seconds=7,
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    _, bearer = auth
    _, key = await api_project_key(client, bearer)
    event = {"type": "order.created", "payload": {"id": "1"}}
    first_headers = {"X-API-Key": key, "Idempotency-Key": "event-0001"}
    first = await client.post("/v1/events", headers=first_headers, json=event)
    repeated = await client.post("/v1/events", headers=first_headers, json=event)
    denied = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "event-0002"},
        json=event,
    )
    assert first.status_code == repeated.status_code == 202
    assert first.json()["public_id"] == repeated.json()["public_id"]
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert denied.json()["error"]["details"]["quota"] == "event_rate"
    assert 1 <= int(denied.headers["Retry-After"]) <= 7


@pytest.mark.asyncio
async def test_endpoint_and_fanout_quotas(
    client: httpx.AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth

    endpoint_settings = phase4_settings(tenant_endpoints_per_project=1)
    monkeypatch.setattr(
        factory_module, "get_settings", lambda: endpoint_settings
    )
    project_id, _ = await api_project_key(client, bearer)
    first = await create_api_endpoint(client, bearer, project_id, "one")
    denied = await create_api_endpoint(client, bearer, project_id, "two")
    assert first.status_code == 201
    assert denied.status_code == 429
    assert denied.json()["error"]["details"]["quota"] == (
        "endpoints_per_project"
    )
    endpoint_url = (
        f"/v1/projects/{project_id}/endpoints/"
        f"{first.json()['public_id']}"
    )
    deactivated = await client.patch(
        endpoint_url,
        headers=bearer,
        json={"is_active": False},
    )
    reactivated = await client.patch(
        endpoint_url,
        headers=bearer,
        json={"is_active": True},
    )
    assert deactivated.status_code == 200
    assert reactivated.status_code == 200

    fanout_settings = phase4_settings(
        tenant_endpoints_per_project=2,
        tenant_fanout_per_event=1,
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: fanout_settings)
    fanout_project, key = await api_project_key(client, bearer)
    assert (
        await create_api_endpoint(client, bearer, fanout_project, "one")
    ).status_code == 201
    assert (
        await create_api_endpoint(client, bearer, fanout_project, "two")
    ).status_code == 201
    event = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "fanout-0001"},
        json={"type": "fanout.test", "payload": {}},
    )
    assert event.status_code == 429
    assert event.json()["error"]["details"]["quota"] == "fanout_per_event"


@pytest.mark.asyncio
async def test_global_backlog_returns_503_without_retry_after(
    client: httpx.AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = phase4_settings(global_max_backlog=1)
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    project_id, key = await api_project_key(client, bearer)
    endpoint = await create_api_endpoint(client, bearer, project_id, "hook")
    assert endpoint.status_code == 201
    first = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "backlog-0001"},
        json={"type": "backlog.test", "payload": {}},
    )
    denied = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "backlog-0002"},
        json={"type": "backlog.test", "payload": {}},
    )
    assert first.status_code == 202
    assert denied.status_code == 503
    assert denied.json()["error"]["code"] == "SERVICE_SATURATED"
    assert denied.json()["error"]["details"]["reason"] == "global_backlog"
    assert "Retry-After" not in denied.headers


@pytest.mark.asyncio
async def test_retained_bytes_and_replay_rate_quotas(
    client: httpx.AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    retained_settings = phase4_settings(
        webhook_payload_max_bytes=900,
        tenant_retained_bytes=1_024,
    )
    monkeypatch.setattr(
        factory_module, "get_settings", lambda: retained_settings
    )
    _, key = await api_project_key(client, bearer)
    body = {"type": "retained.test", "payload": {"data": "x" * 700}}
    first = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "retained-0001"},
        json=body,
    )
    denied = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "retained-0002"},
        json=body,
    )
    assert first.status_code == 202
    assert denied.status_code == 429
    assert denied.json()["error"]["details"]["quota"] == "retained_bytes"

    replay_settings = phase4_settings(
        tenant_replay_burst=1,
        tenant_replay_rate_per_second=0.001,
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: replay_settings)
    project_id, replay_key = await api_project_key(client, bearer)
    endpoint = await create_api_endpoint(client, bearer, project_id, "replay")
    assert endpoint.status_code == 201
    accepted = await client.post(
        "/v1/events",
        headers={"X-API-Key": replay_key, "Idempotency-Key": "replay-0001"},
        json={"type": "replay.test", "payload": {}},
    )
    assert accepted.status_code == 202
    deliveries = await client.get(
        f"/v1/projects/{project_id}/deliveries", headers=bearer
    )
    delivery_id = deliveries.json()[0]["public_id"]
    replay_url = f"/v1/projects/{project_id}/deliveries/{delivery_id}/replay"
    first_replay = await client.post(replay_url, headers=bearer)
    denied_replay = await client.post(replay_url, headers=bearer)
    assert first_replay.status_code == 201
    assert denied_replay.status_code == 429
    assert denied_replay.json()["error"]["details"]["quota"] == "replay_rate"


def test_replica_pool_and_global_worker_budgets_are_validated():
    base = {
        "_env_file": None,
        "environment": "test",
        "secret_key": "s" * 32,
        "api_key_pepper": "p" * 32,
        "webhook_signing_key": "w" * 32,
    }
    with pytest.raises(ValueError, match="DATABASE_CONNECTION_BUDGET"):
        Settings(**base, api_replica_count=2, database_connection_budget=20)
    with pytest.raises(ValueError, match="egress budget"):
        Settings(
            **base,
            database_connection_budget=30,
            worker_replica_count=2,
            worker_global_concurrency=11,
        )


async def seed_queue(
    factory: async_sessionmaker[AsyncSession],
    name: str,
    endpoint_delivery_counts: list[int],
) -> tuple[int, list[int]]:
    now = datetime.now(timezone.utc)
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
            endpoint_ids = []
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
                    session.add(
                        Delivery(
                            public_id=str(uuid4()),
                            organization_id=organization.id,
                            event_id=event.id,
                            endpoint_id=endpoint.id,
                            endpoint_public_id_snapshot=endpoint.public_id,
                            endpoint_url_snapshot=endpoint.url,
                            endpoint_active_snapshot=True,
                            signing_secret_version_snapshot=1,
                            status="pending",
                            attempt_count=0,
                            next_attempt_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            return organization.id, endpoint_ids


@pytest.mark.asyncio
async def test_claiming_rotates_tenants_and_endpoints(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    noisy_id, _ = await seed_queue(
        sqlite_session_factory, "noisy", [6]
    )
    healthy_id, _ = await seed_queue(
        sqlite_session_factory, "healthy", [6]
    )
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            sqlite_session_factory, client, phase4_settings()
        )
        claims = await service.claim_due(4)
    assert len(claims) == 4
    assert [claim.organization_id for claim in claims].count(noisy_id) == 2
    assert [claim.organization_id for claim in claims].count(healthy_id) == 2


@pytest.mark.asyncio
async def test_claiming_rotates_endpoints_within_tenant(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    _, endpoint_ids = await seed_queue(
        sqlite_session_factory, "endpoint-fair", [6, 6]
    )
    async with httpx.AsyncClient() as client:
        service = DeliveryService(
            sqlite_session_factory, client, phase4_settings()
        )
        claims = await service.claim_due(4)
    assert len(claims) == 4
    for endpoint_id in endpoint_ids:
        assert [claim.endpoint_id for claim in claims].count(endpoint_id) == 2


@pytest.mark.asyncio
async def test_global_tenant_endpoint_and_rate_claim_caps(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    _, endpoint_ids = await seed_queue(
        sqlite_session_factory, "claim-caps", [5]
    )
    settings = phase4_settings(
        worker_global_concurrency=2,
        tenant_in_flight_deliveries=2,
        endpoint_concurrency=2,
        endpoint_rate_burst=1,
        endpoint_rate_per_second=0.001,
    )
    async with httpx.AsyncClient() as client:
        service = DeliveryService(sqlite_session_factory, client, settings)
        first = await service.claim_due(5)
        second = await service.claim_due(5)
    assert len(first) == 1
    assert first[0].endpoint_id == endpoint_ids[0]
    assert second == []


@pytest.mark.asyncio
async def test_expired_processing_lease_counts_toward_oldest_due_age(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    organization_id, _ = await seed_queue(
        sqlite_session_factory, "expired-lease", [1]
    )
    async with sqlite_session_factory() as session:
        async with session.begin():
            delivery = await session.scalar(select(Delivery))
            assert delivery is not None
            delivery.status = "processing"
            delivery.lease_expires_at = datetime.now(
                timezone.utc
            ) - timedelta(seconds=60)
            delivery.next_attempt_at = datetime.now(
                timezone.utc
            ) + timedelta(hours=1)

    settings = phase4_settings(
        global_oldest_due_admission_seconds=10.0
    )
    async with sqlite_session_factory() as session:
        with pytest.raises(SaturationError) as raised:
            await AdmissionController(session, settings).admit_event(
                organization_id,
                delivery_count=0,
                envelope_size=2,
            )
    assert raised.value.details["reason"] == "oldest_due_age"