"""Regression tests for API-key lookup-prefix collisions.

``api_keys.key_prefix`` is unique, so a random 48-bit prefix collision at
creation or rotation time must be retried transparently instead of leaking
an integrity error, and authentication must verify the digest rather than
trusting a prefix match.
"""

import pytest
from httpx import AsyncClient

import app.services.webhook_service as webhook_service_module
from app.tests.test_webhook_idempotency import create_project_key
from app.webhook_security import generate_api_key


@pytest.mark.asyncio
async def test_prefix_match_with_wrong_secret_is_refused(
    client: AsyncClient,
    auth,
):
    _, bearer_headers = auth
    api_key = await create_project_key(client, bearer_headers)
    prefix = api_key.split("_", 2)[1]

    response = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": f"whk_{prefix}_{'A' * 43}",
            "Idempotency-Key": "collision-wrong-secret",
        },
        json={"type": "order.created", "payload": {"n": 1}},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_creation_retries_colliding_prefix(
    client: AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    _, bearer_headers = auth
    first_key = await create_project_key(client, bearer_headers)
    taken_prefix = first_key.split("_", 2)[1]

    real_generate = generate_api_key
    calls = {"count": 0}

    def collide_once() -> tuple[str, str]:
        calls["count"] += 1
        if calls["count"] == 1:
            plaintext, _ = real_generate()
            secret = plaintext.split("_", 2)[2]
            return f"whk_{taken_prefix}_{secret}", taken_prefix
        return real_generate()

    monkeypatch.setattr(
        webhook_service_module, "generate_api_key", collide_once
    )

    organization = await client.get(
        "/v1/organizations", headers=bearer_headers
    )
    project_list = await client.get(
        f"/v1/organizations/{organization.json()[0]['public_id']}/projects",
        headers=bearer_headers,
    )
    project_id = project_list.json()[0]["public_id"]

    created = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=bearer_headers,
        json={"name": "collision-retry"},
    )
    assert created.status_code == 201, created.text
    assert calls["count"] >= 2
    new_key = created.json()["plaintext_key"]
    assert new_key.split("_", 2)[1] != taken_prefix

    response = await client.post(
        "/v1/events",
        headers={"X-API-Key": new_key, "Idempotency-Key": "collision-ok"},
        json={"type": "order.created", "payload": {"n": 2}},
    )
    assert response.status_code == 202, response.text


@pytest.mark.asyncio
async def test_creation_exhausted_prefix_allocation_conflicts(
    client: AsyncClient,
    auth,
    monkeypatch: pytest.MonkeyPatch,
):
    _, bearer_headers = auth
    first_key = await create_project_key(client, bearer_headers)
    taken_prefix = first_key.split("_", 2)[1]

    def always_collide() -> tuple[str, str]:
        plaintext, _ = generate_api_key()
        secret = plaintext.split("_", 2)[2]
        return f"whk_{taken_prefix}_{secret}", taken_prefix

    monkeypatch.setattr(
        webhook_service_module, "generate_api_key", always_collide
    )

    organization = await client.get(
        "/v1/organizations", headers=bearer_headers
    )
    project_list = await client.get(
        f"/v1/organizations/{organization.json()[0]['public_id']}/projects",
        headers=bearer_headers,
    )
    project_id = project_list.json()[0]["public_id"]

    created = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=bearer_headers,
        json={"name": "collision-exhausted"},
    )
    assert created.status_code == 409, created.text
