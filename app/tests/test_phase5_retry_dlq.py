from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api_key_auth as api_key_auth_module
import app.services.delivery_service as delivery_module
import app.services.factory as factory_module
import app.services.webhook_service as webhook_module
from app.models import (
    Delivery,
    EndpointQuotaState,
    OrganizationMember,
    ReplayOperation,
)
from app.retry_policy import is_retryable_status, parse_retry_after
from app.services.delivery_service import ClaimedDelivery, DeliveryService
from app.tests.test_delivery_contracts import seed_delivery, worker_settings
from app.tests.test_phase4_admission import (
    api_project_key,
    create_api_endpoint,
)


@pytest.fixture(autouse=True)
def align_api_key_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        api_key_auth_module,
        "get_settings",
        lambda: factory_module.get_settings(),
    )


async def allow_target(url: str, allow_http: bool = False) -> str:
    del allow_http
    return url


def test_retry_classification_and_retry_after_parsing():
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    for status_code in (408, 425, 429, 500, 503, 599):
        assert is_retryable_status(status_code)
    for status_code in (200, 301, 400, 404, 499, 600):
        assert not is_retryable_status(status_code)
    assert parse_retry_after("15", now, 60) == 15
    assert parse_retry_after("999", now, 60) == 60
    assert parse_retry_after("Thu, 20 Aug 2026 12:00:30 GMT", now, 60) == 30
    assert parse_retry_after("invalid", now, 60) is None


@pytest.mark.asyncio
async def test_max_age_skips_outbound_request():
    called = False

    async def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    claim = ClaimedDelivery(
        id=1,
        public_id=str(uuid4()),
        organization_id=1,
        endpoint_id=1,
        lease_token=str(uuid4()),
        attempt_number=1,
        endpoint_public_id=str(uuid4()),
        endpoint_url="https://receiver.example/hook",
        endpoint_secret_version=1,
        endpoint_active=True,
        event_public_id=str(uuid4()),
        event_type="max-age.test",
        canonical_envelope=b"{}",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    settings = worker_settings().model_copy(
        update={"webhook_max_delivery_age_seconds": 60}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(
            None,  # type: ignore[arg-type]
            client,
            settings,
        )
        result = await service._perform_attempt(claim)
    assert result.error == "max_delivery_age"
    assert result.retryable is False
    assert called is False


@pytest.mark.asyncio
async def test_retry_after_and_circuit_half_open_recovery(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    del db_session
    await seed_delivery(sqlite_session_factory)
    settings = worker_settings().model_copy(
        update={
            "endpoint_circuit_failure_threshold": 2,
            "endpoint_circuit_open_seconds": 60,
            "endpoint_retry_burst": 10,
            "webhook_max_delivery_age_seconds": 3_600,
        }
    )
    monkeypatch.setattr(delivery_module, "jittered_backoff", lambda *args: 0.0)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(sqlite_session_factory, client, settings)
        first = (await service.claim_due(1))[0]
        assert await service._finalize(
            first,
            datetime.now(timezone.utc),
            False,
            True,
            503,
            "retryable_http_status",
            "busy",
            retry_after_seconds=30,
        )
        async with sqlite_session_factory() as session:
            delivery = await session.get(Delivery, first.id)
            assert delivery is not None
            assert 29 <= (
                delivery.next_attempt_at - delivery.updated_at
            ).total_seconds() <= 31
            delivery.next_attempt_at = datetime.now(timezone.utc)
            await session.commit()

        second = (await service.claim_due(1))[0]
        assert await service._finalize(
            second,
            datetime.now(timezone.utc),
            False,
            True,
            503,
            "retryable_http_status",
            "busy",
        )

        assert await service.claim_due(1) == []
        async with sqlite_session_factory() as session:
            state = await session.scalar(select(EndpointQuotaState))
            delivery = await session.get(Delivery, first.id)
            assert state is not None and delivery is not None
            assert state.circuit_state == "open"
            state.circuit_open_until = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
            delivery.next_attempt_at = datetime.now(timezone.utc)
            await session.commit()

        probe = (await service.claim_due(1))[0]
        async with sqlite_session_factory() as session:
            state = await session.scalar(select(EndpointQuotaState))
            assert state is not None
            assert state.circuit_state == "half_open"
            assert state.half_open_probe_delivery_id == probe.id
        assert await service._finalize(
            probe,
            datetime.now(timezone.utc),
            True,
            False,
            200,
            None,
            "ok",
        )
    async with sqlite_session_factory() as session:
        state = await session.scalar(select(EndpointQuotaState))
        assert state is not None
        assert state.circuit_state == "closed"
        assert state.consecutive_failures == 0


@pytest.mark.asyncio
async def test_dead_operations_replay_pause_cancel_export_and_purge(
    client: httpx.AsyncClient,
    auth,
    other_auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = worker_settings().model_copy(
        update={
            "delivery_retention_days": 1,
            "delivery_purge_batch_size": 100,
        }
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    other_user, other_bearer = other_auth
    project_id, api_key = await api_project_key(client, bearer)
    endpoint = await create_api_endpoint(
        client, bearer, project_id, "phase5"
    )
    assert endpoint.status_code == 201
    endpoint_id = endpoint.json()["public_id"]
    accepted = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": api_key,
            "Idempotency-Key": "phase5-event-0001",
        },
        json={"type": "phase5.test", "payload": {}},
    )
    assert accepted.status_code == 202
    delivery = await db_session.scalar(select(Delivery))
    assert delivery is not None
    delivery.status = "dead"
    delivery.dead_reason = "max_attempts"
    delivery.dead_at = datetime.now(timezone.utc) - timedelta(days=2)
    delivery.updated_at = delivery.dead_at
    db_session.add(
        OrganizationMember(
            organization_id=delivery.organization_id,
            user_id=other_user["id"],
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    delivery_id = delivery.public_id

    dead = await client.get(
        f"/v1/projects/{project_id}/dead-deliveries",
        headers=bearer,
        params={"reason": "max_attempts", "minimum_age_seconds": 60},
    )
    assert dead.status_code == 200
    assert [row["public_id"] for row in dead.json()] == [delivery_id]
    exported = await client.get(
        f"/v1/projects/{project_id}/dead-deliveries/export",
        headers=bearer,
    )
    assert exported.status_code == 200
    assert delivery_id in exported.text
    assert "max_attempts" in exported.text

    replay_url = f"/v1/projects/{project_id}/replays"
    replay_headers = {**bearer, "Idempotency-Key": "replay-batch-0001"}
    replay_body = {"delivery_ids": [delivery_id]}
    replay = await client.post(
        replay_url, headers=replay_headers, json=replay_body
    )
    repeated = await client.post(
        replay_url, headers=replay_headers, json=replay_body
    )
    assert replay.status_code == repeated.status_code == 201
    assert replay.json()["public_id"] == repeated.json()["public_id"]
    assert replay.json()["mode"] == "single"
    assert replay.json()["created_count"] == 1
    assert await db_session.scalar(
        select(func.count(ReplayOperation.id))
    ) == 1
    assert await db_session.scalar(select(func.count(Delivery.id))) == 2

    paused = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}/pause",
        headers=bearer,
        json={"reason": "maintenance"},
    )
    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    replay_delivery_id = replay.json()["created_delivery_ids"][0]
    canceled = await client.post(
        f"/v1/projects/{project_id}/deliveries/{replay_delivery_id}/cancel",
        headers=bearer,
        json={"reason": "operator request"},
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    resumed = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}/resume",
        headers=bearer,
    )
    recovered = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}/recover-circuit",
        headers=bearer,
    )
    assert resumed.status_code == recovered.status_code == 200
    assert resumed.json()["paused"] is False
    assert recovered.json()["circuit_state"] == "closed"

    denied_purge = await client.post(
        f"/v1/projects/{project_id}/deliveries/purge",
        headers=other_bearer,
        json={"dry_run": True, "max_records": 100},
    )
    assert denied_purge.status_code == 404
    preview = await client.post(
        f"/v1/projects/{project_id}/deliveries/purge",
        headers=bearer,
        json={"dry_run": True, "max_records": 100},
    )
    purged = await client.post(
        f"/v1/projects/{project_id}/deliveries/purge",
        headers=bearer,
        json={"dry_run": False, "max_records": 100},
    )
    assert preview.status_code == purged.status_code == 200
    assert preview.json()["matched"] == 1
    assert preview.json()["purged"] == 0
    assert purged.json()["purged"] == 1
