import json
from uuid import uuid4

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

import app.services.delivery_service as delivery_module
from app.models import Event, Project
from app.services.delivery_service import ClaimedDelivery, DeliveryService
from app.tests.test_delivery_contracts import worker_settings
from app.tests.test_webhook_idempotency import create_project_key


@pytest.mark.asyncio
async def test_cloudevents_mode_is_additive_and_idempotency_safe(
    client: AsyncClient,
    auth,
    db_session: AsyncSession,
):
    _, bearer_headers = auth
    api_key = await create_project_key(client, bearer_headers)
    headers = {
        "X-API-Key": api_key,
        "Idempotency-Key": "phase8-cloud-event",
    }
    body = {
        "type": "order.created",
        "payload": {"order_id": "42"},
        "envelope_mode": "cloudevents",
    }

    accepted = await client.post("/v1/events", headers=headers, json=body)
    repeated = await client.post("/v1/events", headers=headers, json=body)
    changed_mode = await client.post(
        "/v1/events",
        headers=headers,
        json={"type": body["type"], "payload": body["payload"]},
    )

    assert accepted.status_code == repeated.status_code == 202
    assert accepted.json()["envelope_mode"] == "cloudevents"
    assert repeated.json()["public_id"] == accepted.json()["public_id"]
    assert changed_mode.status_code == 409

    event = await db_session.scalar(
        select(Event).where(Event.public_id == accepted.json()["public_id"])
    )
    assert event is not None
    project = await db_session.get(Project, event.project_id)
    assert project is not None
    envelope = json.loads(bytes(event.canonical_envelope))
    envelope_time = envelope.pop("time")
    assert isinstance(envelope_time, str)
    assert envelope == {
        "data": {"order_id": "42"},
        "datacontenttype": "application/json",
        "id": event.public_id,
        "source": f"urn:webhook-platform:project:{project.public_id}",
        "specversion": "1.0",
        "type": "order.created",
    }


@pytest.mark.asyncio
async def test_cloudevents_delivery_uses_structured_media_type(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async def allow_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    requests: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(
        delivery_module, "validate_webhook_url", allow_target
    )
    claim = ClaimedDelivery(
        id=1,
        public_id=str(uuid4()),
        organization_id=1,
        endpoint_id=1,
        lease_token=str(uuid4()),
        attempt_number=1,
        endpoint_public_id=str(uuid4()),
        endpoint_url="https://receiver.example/webhooks",
        endpoint_secret_version=1,
        endpoint_active=True,
        event_public_id=str(uuid4()),
        event_type="order.created",
        canonical_envelope=b'{"specversion":"1.0"}',
        envelope_mode="cloudevents",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as http_client:
        service = DeliveryService(
            sqlite_session_factory, http_client, worker_settings()
        )
        result = await service._perform_attempt(claim)

    assert result.succeeded is True
    assert requests[0].headers["Content-Type"] == (
        "application/cloudevents+json"
    )


@pytest.mark.asyncio
async def test_portal_assets_are_same_origin_and_csp_guarded(
    client: AsyncClient,
):
    page = await client.get("/portal")
    script = await client.get("/portal/app.js")
    styles = await client.get("/portal/styles.css")

    assert page.status_code == script.status_code == styles.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    policy = page.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "'unsafe-inline'" not in policy
    assert 'src="/portal/app.js"' in page.text
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
