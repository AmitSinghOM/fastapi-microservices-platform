"""Regression tests for scoped machine (API key) credentials.

Covers the opt-in machine-auth surface added to the management API:
endpoints create/list/update/delete/rotate-secret, deliveries list/detail,
dead-deliveries list, idempotent batch replay, and delivery recovery
assessment. All other management routes (including the legacy single
delivery replay and API key management itself) remain JWT-only. See
app/api_key_auth.py, app/authorization.py and app/principals.py for the
contract this file exercises.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

import app.api_key_auth as api_key_auth_module
import app.services.factory as factory_module
import app.services.webhook_service as webhook_module
from app.models import (
    AdministrativeAuditEvent,
    ApiKey,
    Delivery,
    Event,
    EndpointQuotaState,
    EndpointSigningSecretVersion,
    WebhookEndpoint,
)
from app.tests.test_delivery_contracts import worker_settings
from app.tests.test_phase7_tenant_lifecycle import allow_target, create_tenant


@pytest.fixture(autouse=True)
def align_settings(monkeypatch: pytest.MonkeyPatch):
    """Keep API-key authentication and the service layer on one settings
    instance, and let endpoint URL validation accept test hostnames."""
    settings = worker_settings()
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_key_auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)


async def make_key(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    scopes: list[str] | None = None,
    *,
    name: str = "machine",
    expires_in_days: int | None = None,
) -> dict:
    payload: dict[str, object] = {"name": name}
    if scopes is not None:
        payload["scopes"] = scopes
    if expires_in_days is not None:
        payload["expires_in_days"] = expires_in_days
    response = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def api_key_headers(plaintext: str) -> dict[str, str]:
    return {"X-API-Key": plaintext}


async def make_endpoint(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_id: str,
    url: str = "https://receiver.example/machine",
) -> dict:
    response = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=headers,
        json={"url": url},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def ingest_event(
    client: httpx.AsyncClient,
    plaintext_key: str,
    idempotency_key: str,
    event_type: str = "order.created",
) -> dict:
    response = await client.post(
        "/v1/events",
        headers={
            **api_key_headers(plaintext_key),
            "Idempotency-Key": idempotency_key,
        },
        json={"type": event_type, "payload": {"ok": True}},
    )
    assert response.status_code == 202, response.text
    return response.json()


ALL_SCOPES = [
    "events:write",
    "endpoints:read",
    "endpoints:write",
    "deliveries:read",
    "deliveries:replay",
]


# ---------------------------------------------------------------------------
# Opt-in scoped routes: happy-path lifecycle with a fully-scoped credential.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_scope_credential_can_drive_endpoint_and_delivery_lifecycle(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ALL_SCOPES)
    machine = api_key_headers(key["plaintext_key"])

    created = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=machine,
        json={"url": "https://receiver.example/full-scope"},
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["public_id"]

    listed = await client.get(
        f"/v1/projects/{project_id}/endpoints", headers=machine
    )
    assert listed.status_code == 200
    assert any(e["public_id"] == endpoint_id for e in listed.json())

    updated = await client.patch(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}",
        headers=machine,
        json={"description": "updated by machine"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "updated by machine"

    rotated = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}/rotate-secret",
        headers=machine,
    )
    assert rotated.status_code == 200, rotated.text

    accepted = await ingest_event(client, key["plaintext_key"], "lifecycle-1")
    assert accepted["event_type"] == "order.created"

    deliveries = await client.get(
        f"/v1/projects/{project_id}/deliveries", headers=machine
    )
    assert deliveries.status_code == 200
    assert len(deliveries.json()) == 1
    delivery_public_id = deliveries.json()[0]["public_id"]

    detail = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery_public_id}",
        headers=machine,
    )
    assert detail.status_code == 200, detail.text

    dead_list = await client.get(
        f"/v1/projects/{project_id}/dead-deliveries", headers=machine
    )
    assert dead_list.status_code == 200
    assert dead_list.json() == []

    recovery = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery_public_id}/recovery",
        headers=machine,
    )
    assert recovery.status_code == 200, recovery.text
    assert recovery.json()["snapshot_preserved"] is True

    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()

    replay = await client.post(
        f"/v1/projects/{project_id}/replays",
        headers={**machine, "Idempotency-Key": "replay-lifecycle"},
        json={"delivery_ids": [delivery_public_id]},
    )
    assert replay.status_code == 201, replay.text

    deactivated = await client.delete(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}", headers=machine
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Negative: wrong project -> 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_against_wrong_project_is_404(client, auth):
    _, headers = auth
    _, project_a = await create_tenant(client, headers)
    _, project_b = await create_tenant(client, headers)
    key = await make_key(client, headers, project_a, ["endpoints:read"])
    machine = api_key_headers(key["plaintext_key"])

    response = await client.get(
        f"/v1/projects/{project_b}/endpoints", headers=machine
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_api_key_against_unknown_project_is_404(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:read"])
    machine = api_key_headers(key["plaintext_key"])

    response = await client.get(
        "/v1/projects/does-not-exist/endpoints", headers=machine
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Negative: missing scope -> 403, and endpoints:write does not imply read.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_scope_is_403(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["events:write"])
    machine = api_key_headers(key["plaintext_key"])

    response = await client.get(
        f"/v1/projects/{project_id}/endpoints", headers=machine
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_endpoints_write_does_not_imply_read(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:write"])
    machine = api_key_headers(key["plaintext_key"])

    created = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=machine,
        json={"url": "https://receiver.example/write-only"},
    )
    assert created.status_code == 201, created.text

    denied_list = await client.get(
        f"/v1/projects/{project_id}/endpoints", headers=machine
    )
    assert denied_list.status_code == 403

    denied_get_delivery_scope = await client.get(
        f"/v1/projects/{project_id}/deliveries", headers=machine
    )
    assert denied_get_delivery_scope.status_code == 403


@pytest.mark.asyncio
async def test_deliveries_read_does_not_imply_replay(client, auth, db_session):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "read-only-replay")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    denied = await client.post(
        f"/v1/projects/{project_id}/replays",
        headers={**machine, "Idempotency-Key": "denied-replay"},
        json={"delivery_ids": [delivery.public_id]},
    )
    assert denied.status_code == 403


# ---------------------------------------------------------------------------
# Negative: invalid/expired/revoked key, inactive project, deletion-pending
# organization all fail with 401 (indistinguishable from an invalid key).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_garbage_api_key_is_401(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    response = await client.get(
        f"/v1/projects/{project_id}/endpoints",
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_api_key_is_401(client, auth, db_session):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(
        client, headers, project_id, ["endpoints:read"], expires_in_days=1
    )
    stored = await db_session.scalar(
        select(ApiKey).where(ApiKey.public_id == key["public_id"])
    )
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    response = await client.get(
        f"/v1/projects/{project_id}/endpoints",
        headers=api_key_headers(key["plaintext_key"]),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_revoked_api_key_is_401(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:read"])

    revoked = await client.delete(
        f"/v1/projects/{project_id}/api-keys/{key['public_id']}",
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text

    response = await client.get(
        f"/v1/projects/{project_id}/endpoints",
        headers=api_key_headers(key["plaintext_key"]),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_inactive_project_rejects_api_key_401(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:read"])

    deactivated = await client.delete(
        f"/v1/projects/{project_id}", headers=headers
    )
    assert deactivated.status_code == 200, deactivated.text

    response = await client.get(
        f"/v1/projects/{project_id}/endpoints",
        headers=api_key_headers(key["plaintext_key"]),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_deletion_pending_organization_rejects_api_key_401(client, auth):
    _, headers = auth
    organization_id, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:read"])

    requested = await client.delete(
        f"/v1/organizations/{organization_id}", headers=headers
    )
    assert requested.status_code == 202, requested.text

    response = await client.get(
        f"/v1/projects/{project_id}/endpoints",
        headers=api_key_headers(key["plaintext_key"]),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_inactive_api_key_is_401(client, auth, db_session):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:read"])
    stored = await db_session.scalar(
        select(ApiKey).where(ApiKey.public_id == key["public_id"])
    )
    stored.is_active = False
    await db_session.commit()

    response = await client.get(
        f"/v1/projects/{project_id}/endpoints",
        headers=api_key_headers(key["plaintext_key"]),
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Negative: mixed JWT + API key credentials on opt-in routes are rejected,
# while legacy JWT-only routes refuse an API key outright.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_jwt_and_api_key_rejected_on_opt_in_route(client, auth):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:read"])

    mixed_headers = {**headers, **api_key_headers(key["plaintext_key"])}
    response = await client.get(
        f"/v1/projects/{project_id}/endpoints", headers=mixed_headers
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_legacy_single_replay_is_jwt_only(client, auth, db_session):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:replay"]
    )
    await ingest_event(client, key["plaintext_key"], "legacy-replay")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()

    denied = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=api_key_headers(key["plaintext_key"]),
    )
    assert denied.status_code == 401, denied.text

    accepted = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text


@pytest.mark.asyncio
async def test_key_management_routes_remain_jwt_only(client, auth):
    """A machine credential cannot create, list, revoke or rotate keys:
    no scope grants key-management, so this is a no-escalation guarantee."""
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ALL_SCOPES)
    machine = api_key_headers(key["plaintext_key"])

    denied_create = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=machine,
        json={"name": "escalation-attempt"},
    )
    assert denied_create.status_code == 401

    denied_list = await client.get(
        f"/v1/projects/{project_id}/api-keys", headers=machine
    )
    assert denied_list.status_code == 401

    denied_revoke = await client.delete(
        f"/v1/projects/{project_id}/api-keys/{key['public_id']}",
        headers=machine,
    )
    assert denied_revoke.status_code == 401

    denied_rotate = await client.post(
        f"/v1/projects/{project_id}/api-keys/{key['public_id']}/rotate",
        headers=machine,
    )
    assert denied_rotate.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_runtime_and_delivery_mutation_routes_remain_jwt_only(
    client, auth, db_session
):
    """pause/resume/recover-circuit/cancel/purge are not opt-in: a fully
    scoped machine credential must still be refused on these routes."""
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    endpoint = await make_endpoint(client, headers, project_id)
    key = await make_key(client, headers, project_id, ALL_SCOPES)
    machine = api_key_headers(key["plaintext_key"])

    for response in (
        await client.post(
            f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}/pause",
            headers=machine,
            json={},
        ),
        await client.post(
            f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}/resume",
            headers=machine,
        ),
        await client.post(
            f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}/recover-circuit",
            headers=machine,
        ),
        await client.post(
            f"/v1/projects/{project_id}/deliveries/purge",
            headers=machine,
            json={"dry_run": True, "max_records": 10},
        ),
    ):
        assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Delivery read: attempt response bodies are present; event payload and
# endpoint URL are never exposed through the machine-facing routes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_detail_excludes_payload_and_url_but_keeps_responses(
    client, auth, db_session, sqlite_session_factory
):
    import app.services.delivery_service as delivery_module
    from app.services.delivery_service import DeliveryService

    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "detail-body")

    def receiver(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="downstream body")

    monkeypatch_target = delivery_module.validate_webhook_url
    delivery_module.validate_webhook_url = allow_target
    try:
        transport = httpx.MockTransport(receiver)
        async with httpx.AsyncClient(transport=transport) as outbound:
            service = DeliveryService(
                sqlite_session_factory, outbound, worker_settings()
            )
            claim = (await service.claim_due(1))[0]
            await service.deliver(claim)
    finally:
        delivery_module.validate_webhook_url = monkeypatch_target

    delivery = await db_session.scalar(select(Delivery))
    machine = api_key_headers(key["plaintext_key"])

    detail = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}",
        headers=machine,
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "payload" not in body
    assert "url" not in body
    assert "endpoint_url_snapshot" not in body
    assert body["attempts"], "expected at least one attempt"
    assert body["attempts"][0]["response_body"] == "downstream body"


# ---------------------------------------------------------------------------
# Audit: machine actions record actor_user_id=None and api_key metadata,
# never the raw credential.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_machine_action_audit_metadata_has_no_user_and_no_raw_secret(
    client, auth, db_session
):
    _, headers = auth
    organization_id, project_id = await create_tenant(client, headers)
    key = await make_key(client, headers, project_id, ["endpoints:write"])
    machine = api_key_headers(key["plaintext_key"])

    created = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=machine,
        json={"url": "https://receiver.example/audited"},
    )
    assert created.status_code == 201, created.text

    audit_events = list(
        await db_session.scalars(
            select(AdministrativeAuditEvent).where(
                AdministrativeAuditEvent.action == "endpoint.created"
            )
        )
    )
    assert len(audit_events) == 1
    event = audit_events[0]
    assert event.actor_user_id is None
    assert event.sanitized_metadata["actor_type"] == "api_key"
    assert event.sanitized_metadata["actor_id"] == key["public_id"]

    raw_secret = key["plaintext_key"]
    key_prefix = key["key_prefix"]
    dump = str(event.sanitized_metadata)
    assert raw_secret not in dump
    assert key_prefix not in dump

    via_api = await client.get(
        f"/v1/organizations/{organization_id}/audit-events",
        headers=headers,
    )
    assert via_api.status_code == 200
    matching = [e for e in via_api.json() if e["action"] == "endpoint.created"]
    assert matching and matching[0]["actor_user_id"] is None


# ---------------------------------------------------------------------------
# Idempotent batch replay: same key + same deliveries returns the same
# operation; same key + different deliveries conflicts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_replay_is_idempotent_and_audited(
    client, auth, db_session
):
    _, headers = auth
    organization_id, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:replay"]
    )
    await ingest_event(client, key["plaintext_key"], "idempotent-replay")
    await ingest_event(client, key["plaintext_key"], "idempotent-replay-other")
    deliveries = list(await db_session.scalars(select(Delivery)))
    assert len(deliveries) == 2
    delivery, other_delivery = deliveries
    delivery.status = "dead"
    other_delivery.status = "dead"
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    first = await client.post(
        f"/v1/projects/{project_id}/replays",
        headers={**machine, "Idempotency-Key": "same-key"},
        json={"delivery_ids": [delivery.public_id]},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/v1/projects/{project_id}/replays",
        headers={**machine, "Idempotency-Key": "same-key"},
        json={"delivery_ids": [delivery.public_id]},
    )
    assert second.status_code == 201, second.text
    assert second.json()["public_id"] == first.json()["public_id"]

    # Same Idempotency-Key reused with a different (but valid) delivery set
    # must conflict rather than silently create a second replay operation.
    conflicting = await client.post(
        f"/v1/projects/{project_id}/replays",
        headers={**machine, "Idempotency-Key": "same-key"},
        json={"delivery_ids": [other_delivery.public_id]},
    )
    assert conflicting.status_code == 409, conflicting.text

    audit_events = list(
        await db_session.scalars(
            select(AdministrativeAuditEvent).where(
                AdministrativeAuditEvent.action == "delivery.replayed"
            )
        )
    )
    assert len(audit_events) == 1
    assert audit_events[0].actor_user_id is None
    assert audit_events[0].sanitized_metadata["actor_type"] == "api_key"


# ---------------------------------------------------------------------------
# Recovery assessment: read-only, no mutation, surfaces the full blocker and
# warning vocabulary.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_assessment_does_not_mutate_and_blocks_non_dead(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "recovery-pending")
    delivery = await db_session.scalar(select(Delivery))
    assert delivery.status == "pending"
    machine = api_key_headers(key["plaintext_key"])

    recovery = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert recovery.status_code == 200, recovery.text
    body = recovery.json()
    assert body["delivery_id"] == delivery.public_id
    assert body["status"] == "pending"
    assert "delivery_not_dead" in body["blockers"]
    assert body["eligible_for_replay"] is False
    assert body["snapshot_preserved"] is True

    await db_session.refresh(delivery)
    assert delivery.status == "pending", "recovery assessment must not mutate"


@pytest.mark.asyncio
async def test_recovery_assessment_payload_purged_blocker(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "recovery-purged")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    event = await db_session.get(Event, delivery.event_id)
    event.payload = None
    event.canonical_envelope = None
    event.payload_purged_at = datetime.now(timezone.utc)
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    recovery = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert recovery.status_code == 200, recovery.text
    body = recovery.json()
    assert body["status"] == "dead"
    assert "payload_purged" in body["blockers"]
    assert body["eligible_for_replay"] is False


@pytest.mark.asyncio
async def test_recovery_assessment_signing_version_blockers(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "recovery-signing")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    version = await db_session.scalar(select(EndpointSigningSecretVersion))
    await db_session.delete(version)
    await db_session.commit()

    unavailable = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert unavailable.status_code == 200
    assert "signing_version_unavailable" in unavailable.json()["blockers"]

    retired_version = EndpointSigningSecretVersion(
        endpoint_id=delivery.endpoint_id,
        version=delivery.signing_secret_version_snapshot,
        activated_at=datetime.now(timezone.utc) - timedelta(days=2),
        retire_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db_session.add(retired_version)
    await db_session.commit()

    retired = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert retired.status_code == 200
    assert "signing_version_retired" in retired.json()["blockers"]


@pytest.mark.asyncio
async def test_recovery_assessment_warnings_configuration_pause_and_circuit(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    endpoint = await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "recovery-warnings")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    paused = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}/pause",
        headers=headers,
        json={"reason": "operator maintenance"},
    )
    assert paused.status_code == 200, paused.text

    quota_state = await db_session.get(
        EndpointQuotaState,
        (await db_session.get(WebhookEndpoint, delivery.endpoint_id)).id,
    )
    quota_state.circuit_state = "open"
    await db_session.commit()

    updated = await client.patch(
        f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}",
        headers=headers,
        json={"description": "configuration drifted"},
    )
    assert updated.status_code == 200, updated.text

    response = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert response.status_code == 200, response.text
    warnings = set(response.json()["warnings"])
    assert "endpoint_paused" in warnings
    assert "circuit_open" in warnings


@pytest.mark.asyncio
async def test_recovery_assessment_endpoint_inactive_warning(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    endpoint = await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "recovery-inactive")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    deactivated = await client.delete(
        f"/v1/projects/{project_id}/endpoints/{endpoint['public_id']}",
        headers=headers,
    )
    assert deactivated.status_code == 200, deactivated.text

    response = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert response.status_code == 200, response.text
    warnings = set(response.json()["warnings"])
    assert "endpoint_inactive" in warnings
    assert "endpoint_configuration_changed" in warnings


@pytest.mark.asyncio
async def test_recovery_eligible_when_dead_and_unblocked(
    client, auth, db_session
):
    _, headers = auth
    _, project_id = await create_tenant(client, headers)
    await make_endpoint(client, headers, project_id)
    key = await make_key(
        client, headers, project_id, ["events:write", "deliveries:read"]
    )
    await ingest_event(client, key["plaintext_key"], "recovery-eligible")
    delivery = await db_session.scalar(select(Delivery))
    delivery.status = "dead"
    await db_session.commit()
    machine = api_key_headers(key["plaintext_key"])

    response = await client.get(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/recovery",
        headers=machine,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["blockers"] == []
    assert body["warnings"] == []
    assert body["eligible_for_replay"] is True


# ---------------------------------------------------------------------------
# Two customers, same event type: deliveries and keys stay isolated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_customers_same_event_type_are_isolated(
    client, auth, other_auth, db_session
):
    _, owner_headers = auth
    _, outsider_headers = other_auth
    _, project_a = await create_tenant(client, owner_headers)
    _, project_b = await create_tenant(client, outsider_headers)

    await make_endpoint(client, owner_headers, project_a)
    await make_endpoint(client, outsider_headers, project_b)

    key_a = await make_key(
        client, owner_headers, project_a, ["events:write", "deliveries:read"]
    )
    key_b = await make_key(
        client,
        outsider_headers,
        project_b,
        ["events:write", "deliveries:read"],
    )

    event_type = "order.created"
    await ingest_event(
        client, key_a["plaintext_key"], "isolation-a", event_type
    )
    await ingest_event(
        client, key_b["plaintext_key"], "isolation-b", event_type
    )

    deliveries = list(await db_session.scalars(select(Delivery)))
    assert len(deliveries) == 2

    visible_to_a = await client.get(
        f"/v1/projects/{project_a}/deliveries",
        headers=api_key_headers(key_a["plaintext_key"]),
    )
    assert visible_to_a.status_code == 200
    assert len(visible_to_a.json()) == 1

    visible_to_b = await client.get(
        f"/v1/projects/{project_b}/deliveries",
        headers=api_key_headers(key_b["plaintext_key"]),
    )
    assert visible_to_b.status_code == 200
    assert len(visible_to_b.json()) == 1

    assert (
        visible_to_a.json()[0]["public_id"]
        != visible_to_b.json()[0]["public_id"]
    )

    cross_tenant_delivery_id = visible_to_b.json()[0]["public_id"]
    cross_tenant_denied = await client.get(
        f"/v1/projects/{project_a}/deliveries/{cross_tenant_delivery_id}",
        headers=api_key_headers(key_a["plaintext_key"]),
    )
    assert cross_tenant_denied.status_code == 404

    cross_tenant_key_denied = await client.get(
        f"/v1/projects/{project_b}/deliveries",
        headers=api_key_headers(key_a["plaintext_key"]),
    )
    assert cross_tenant_key_denied.status_code == 404
