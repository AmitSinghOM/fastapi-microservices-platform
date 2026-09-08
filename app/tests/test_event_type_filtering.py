"""Per-endpoint event-type subscription filtering.

A NULL filter preserves the historical behavior (every active endpoint
receives every event). A list restricts fan-out at acceptance time to exact
matches and trailing ``prefix.*`` wildcards.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.webhook_service as webhook_module
from app.models import Delivery, WebhookEndpoint
from app.tests.test_phase4_admission import (
    allow_target,
    api_project_key,
)
from app.webhook_security import event_type_matches


def test_event_type_matching_semantics():
    assert event_type_matches("order.created", None)
    assert event_type_matches("order.created", ["order.created"])
    assert not event_type_matches("order.created", ["user.created"])
    assert event_type_matches("order.created", ["order.*"])
    assert event_type_matches("order.refund.done", ["order.*"])
    assert not event_type_matches("order", ["order.*"])
    assert not event_type_matches("orders.created", ["order.*"])
    # Malformed stored state fails closed per entry.
    assert not event_type_matches("order.created", "order.created")
    assert not event_type_matches("order.created", [7])


async def _endpoint(
    client: AsyncClient,
    bearer: dict[str, str],
    project_id: str,
    suffix: str,
    event_types: list[str] | None = None,
) -> dict:
    body: dict = {"url": f"https://receiver.example/{suffix}"}
    if event_types is not None:
        body["event_types"] = event_types
    response = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=bearer,
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _send(
    client: AsyncClient, key: str, idempotency: str, event_type: str
) -> dict:
    response = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": idempotency},
        json={"type": event_type, "payload": {"k": idempotency}},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _delivery_endpoint_ids(
    db: AsyncSession, event_public_id: str
) -> set[str]:
    rows = (
        await db.execute(
            select(Delivery.endpoint_public_id_snapshot)
            .join(
                webhook_module.Event,
                webhook_module.Event.id == Delivery.event_id,
            )
            .where(webhook_module.Event.public_id == event_public_id)
        )
    ).scalars()
    return set(rows)


@pytest.mark.asyncio
async def test_fanout_respects_subscriptions(
    client: AsyncClient,
    auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    project_id, key = await api_project_key(client, bearer)

    unfiltered = await _endpoint(client, bearer, project_id, "all")
    orders_only = await _endpoint(
        client, bearer, project_id, "orders", ["order.*"]
    )
    exact = await _endpoint(
        client, bearer, project_id, "exact", ["user.created"]
    )
    assert unfiltered["event_types"] is None
    assert orders_only["event_types"] == ["order.*"]

    order_event = await _send(client, key, "sub-0001", "order.created")
    user_event = await _send(client, key, "sub-0002", "user.created")
    misc_event = await _send(client, key, "sub-0003", "invoice.paid")

    order_targets = await _delivery_endpoint_ids(
        db_session, order_event["public_id"]
    )
    assert order_targets == {
        unfiltered["public_id"], orders_only["public_id"]
    }

    user_targets = await _delivery_endpoint_ids(
        db_session, user_event["public_id"]
    )
    assert user_targets == {unfiltered["public_id"], exact["public_id"]}

    misc_targets = await _delivery_endpoint_ids(
        db_session, misc_event["public_id"]
    )
    assert misc_targets == {unfiltered["public_id"]}


@pytest.mark.asyncio
async def test_zero_matching_endpoints_still_accepts_event(
    client: AsyncClient,
    auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    project_id, key = await api_project_key(client, bearer)
    await _endpoint(client, bearer, project_id, "orders", ["order.*"])

    accepted = await _send(client, key, "sub-none", "user.created")
    assert (
        await _delivery_endpoint_ids(db_session, accepted["public_id"])
        == set()
    )


@pytest.mark.asyncio
async def test_update_changes_and_clears_filter(
    client: AsyncClient,
    auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    project_id, _ = await api_project_key(client, bearer)
    endpoint = await _endpoint(
        client, bearer, project_id, "mutable", ["order.*"]
    )

    changed = await client.patch(
        f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}",
        headers=bearer,
        json={"event_types": ["user.created", "user.deleted"]},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["event_types"] == ["user.created", "user.deleted"]

    cleared = await client.patch(
        f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}",
        headers=bearer,
        json={"event_types": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["event_types"] is None

    stored = await db_session.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.public_id == endpoint["public_id"]
        )
    )
    assert stored is not None and stored.event_types is None


@pytest.mark.asyncio
async def test_filter_validation_rejects_bad_input(
    client: AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    project_id, _ = await api_project_key(client, bearer)

    for bad in ([], ["*"], ["*.created"], ["or der.created"], ["x" * 151]):
        response = await client.post(
            f"/v1/projects/{project_id}/endpoints",
            headers=bearer,
            json={
                "url": "https://receiver.example/bad",
                "event_types": bad,
            },
        )
        assert response.status_code == 422, (bad, response.text)

    deduped = await _endpoint(
        client, bearer, project_id, "dedupe",
        ["b.two", "a.one", "b.two"],
    )
    assert deduped["event_types"] == ["a.one", "b.two"]
