import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey


async def create_project_key(
    client: AsyncClient,
    headers: dict[str, str],
) -> str:
    organization = await client.post(
        "/v1/organizations",
        headers=headers,
        json={"name": "Phase 0 Organization"},
    )
    assert organization.status_code == 201, organization.text

    project = await client.post(
        f"/v1/organizations/{organization.json()['public_id']}/projects",
        headers=headers,
        json={"name": "Delivery contracts"},
    )
    assert project.status_code == 201, project.text

    api_key = await client.post(
        f"/v1/projects/{project.json()['public_id']}/api-keys",
        headers=headers,
        json={"name": "phase-0-producer"},
    )
    assert api_key.status_code == 201, api_key.text
    return api_key.json()["plaintext_key"]


@pytest.mark.asyncio
async def test_idempotency_reuses_event_and_rejects_changed_content(
    client: AsyncClient,
    auth,
    db_session: AsyncSession,
):
    _, bearer_headers = auth
    api_key = await create_project_key(client, bearer_headers)
    headers = {
        "X-API-Key": api_key,
        "Idempotency-Key": "phase-0-order-0001",
    }
    event = {"type": "order.created", "payload": {"order_id": "0001"}}

    first = await client.post("/v1/events", headers=headers, json=event)
    repeated = await client.post("/v1/events", headers=headers, json=event)
    changed = await client.post(
        "/v1/events",
        headers=headers,
        json={"type": "order.cancelled", "payload": {"order_id": "0001"}},
    )

    assert first.status_code == repeated.status_code == 202
    assert repeated.json()["public_id"] == first.json()["public_id"]
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "CONFLICT"

    key_prefix = api_key.split("_", 2)[1]
    stored_key = await db_session.scalar(
        select(ApiKey).where(ApiKey.key_prefix == key_prefix)
    )
    assert stored_key is not None and stored_key.last_used_at is None
