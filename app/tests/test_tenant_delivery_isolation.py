from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

import app.api_key_auth as key_auth
import app.services.factory as factory_module
import app.services.webhook_service as webhook_module
from app.models import Delivery, EndpointSigningSecretVersion
from app.services.delivery_service import DeliveryService
from app.tests.test_delivery_contracts import now, worker_settings
from app.tests.test_phase7_tenant_lifecycle import allow_target, create_tenant


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_replay_admitted_before_retirement_is_stopped_afterward(
    client, auth, db_session, sqlite_session_factory, monkeypatch, bulk
):
    settings = worker_settings()
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(key_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    endpoint = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=headers,
        json={"url": "https://receiver.example/replay"},
    )
    assert endpoint.status_code == 201
    key = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=headers,
        json={"name": "producer"},
    )
    accepted = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": key.json()["plaintext_key"],
            "Idempotency-Key": "event",
        },
        json={"type": "order.created", "payload": {}},
    )
    assert accepted.status_code == 202
    original = await db_session.scalar(select(Delivery))
    original.status = "dead"
    await db_session.commit()
    rotated = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint.json()['public_id']}/rotate-secret",
        headers=headers,
    )
    assert rotated.status_code == 200
    if bulk:
        response = await client.post(
            f"/v1/projects/{project_id}/replays",
            headers={**headers, "Idempotency-Key": "replay"},
            json={"delivery_ids": [original.public_id]},
        )
    else:
        response = await client.post(
            f"/v1/projects/{project_id}/deliveries/{original.public_id}/replay",
            headers=headers,
        )
    assert response.status_code == 201, response.text
    version = await db_session.scalar(
        select(EndpointSigningSecretVersion).where(
            EndpointSigningSecretVersion.version == 1
        )
    )
    version.retire_at = now() - timedelta(seconds=1)
    await db_session.commit()

    def no_send(request):
        pytest.fail("retired replay was sent")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(no_send)
    ) as outbound:
        service = DeliveryService(sqlite_session_factory, outbound, settings)
        claim = (await service.claim_due(1))[0]
        assert claim.id != original.id
        assert await service.deliver(claim)
    await db_session.refresh(original)
    replay = await db_session.get(Delivery, claim.id)
    assert replay.dead_reason == "signing_version_retired"


@pytest.mark.asyncio
async def test_delivery_operations_hide_other_project_objects(
    client, auth, other_auth, db_session, monkeypatch
):
    settings = worker_settings()
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(key_auth, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, owner = auth
    _, outsider = other_auth
    _, project_id = await create_tenant(client, owner)
    _, other_project_id = await create_tenant(client, outsider)
    endpoint = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=owner,
        json={"url": "https://receiver.example/isolation"},
    )
    assert endpoint.status_code == 201
    key = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=owner,
        json={"name": "producer"},
    )
    accepted = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": key.json()["plaintext_key"],
            "Idempotency-Key": "isolation",
        },
        json={"type": "order.created", "payload": {}},
    )
    assert accepted.status_code == 202
    delivery = await db_session.scalar(select(Delivery))
    for project in (project_id, other_project_id):
        for operation in ("replay", "cancel"):
            denied = await client.post(
                f"/v1/projects/{project}/deliveries/{delivery.public_id}/{operation}",
                headers=outsider,
                json={},
            )
            assert denied.status_code == 404, denied.text
    denied_export = await client.get(
        f"/v1/projects/{project_id}/dead-deliveries/export", headers=outsider
    )
    assert denied_export.status_code == 404
    await db_session.refresh(delivery)
    assert delivery.status == "pending"
