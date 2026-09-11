from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.api_key_auth as api_key_auth_module
import app.services.factory as factory_module
import app.services.webhook_service as webhook_module
from app.models import (
    AdministrativeAuditEvent,
    ApiKey,
    Delivery,
    EndpointSigningSecretVersion,
    Event,
    OrganizationPolicy,
    ProjectMember,
    TenantQuotaState,
)
from app.tests.test_delivery_contracts import worker_settings


@pytest.fixture(autouse=True)
def align_api_key_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        api_key_auth_module,
        "get_settings",
        lambda: factory_module.get_settings(),
    )


async def allow_target(
    url: str, allow_http: bool = False, allow_private: bool = False
) -> str:
    del allow_http, allow_private
    return url


async def create_tenant(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[str, str]:
    organization = await client.post(
        "/v1/organizations", headers=headers, json={"name": "Phase 7"}
    )
    assert organization.status_code == 201, organization.text
    organization_id = organization.json()["public_id"]
    project = await client.post(
        f"/v1/organizations/{organization_id}/projects",
        headers=headers,
        json={"name": "Lifecycle"},
    )
    assert project.status_code == 201, project.text
    return organization_id, project.json()["public_id"]


@pytest.mark.asyncio
async def test_atomic_defaults_and_least_privilege_memberships(
    client: httpx.AsyncClient,
    auth,
    other_auth,
    db_session: AsyncSession,
):
    owner_user, owner_headers = auth
    other_user, other_headers = other_auth
    organization_id, project_id = await create_tenant(client, owner_headers)

    assert await db_session.scalar(select(TenantQuotaState)) is not None
    policy = await db_session.scalar(select(OrganizationPolicy))
    project_member = await db_session.scalar(select(ProjectMember))
    assert policy is not None and policy.plan == "free"
    assert project_member is not None and project_member.role == "admin"

    cross_tenant = await client.get(
        f"/v1/projects/{project_id}/events", headers=other_headers
    )
    assert cross_tenant.status_code == 404

    member = await client.post(
        f"/v1/organizations/{organization_id}/members",
        headers=owner_headers,
        json={"user_id": other_user["id"], "role": "member"},
    )
    assert member.status_code == 201, member.text
    denied_policy = await client.get(
        f"/v1/organizations/{organization_id}/policy",
        headers=other_headers,
    )
    assert denied_policy.status_code == 403
    denied = await client.post(
        f"/v1/organizations/{organization_id}/projects",
        headers=other_headers,
        json={"name": "Denied"},
    )
    assert denied.status_code == 403

    project_member = await client.post(
        f"/v1/projects/{project_id}/members",
        headers=owner_headers,
        json={"user_id": other_user["id"], "role": "viewer"},
    )
    assert project_member.status_code == 201, project_member.text
    visible = await client.get(
        f"/v1/projects/{project_id}/events", headers=other_headers
    )
    denied_member_list = await client.get(
        f"/v1/projects/{project_id}/members", headers=other_headers
    )
    denied_key = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=other_headers,
        json={"name": "denied"},
    )
    assert visible.status_code == 200
    assert denied_member_list.status_code == 403
    assert denied_key.status_code == 403

    updated = await client.patch(
        f"/v1/projects/{project_id}/members/{other_user['id']}",
        headers=owner_headers,
        json={"role": "operator"},
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "operator"
    removed = await client.delete(
        f"/v1/projects/{project_id}/members/{other_user['id']}",
        headers=owner_headers,
    )
    assert removed.status_code == 204
    upserted = await client.put(
        f"/v1/projects/{project_id}/members/{other_user['id']}",
        headers=owner_headers,
        json={"role": "viewer"},
    )
    assert upserted.status_code == 200, upserted.text
    assert upserted.json()["role"] == "viewer"
    upserted_again = await client.put(
        f"/v1/projects/{project_id}/members/{other_user['id']}",
        headers=owner_headers,
        json={"role": "operator"},
    )
    assert upserted_again.status_code == 200, upserted_again.text
    assert upserted_again.json()["role"] == "operator"

    last_owner_demote = await client.patch(
        f"/v1/organizations/{organization_id}/members/{owner_user['id']}",
        headers=owner_headers,
        json={"role": "member"},
    )
    last_owner_remove = await client.delete(
        f"/v1/organizations/{organization_id}/members/{owner_user['id']}",
        headers=owner_headers,
    )
    assert last_owner_demote.status_code == 409
    assert last_owner_remove.status_code == 409


@pytest.mark.asyncio
async def test_key_and_endpoint_rotation_and_replay_safety(
    client: httpx.AsyncClient,
    auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = worker_settings().model_copy(
        update={
            "api_key_default_ttl_days": 2,
            "api_key_rotation_overlap_seconds": 30,
            "endpoint_secret_overlap_seconds": 60,
        }
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, headers = auth
    _, project_id = await create_tenant(client, headers)

    created_key = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=headers,
        json={
            "name": "producer",
            "scopes": ["events:write"],
            "expires_in_days": 2,
        },
    )
    assert created_key.status_code == 201, created_key.text
    key_data = created_key.json()
    assert key_data["scopes"] == ["events:write"]
    assert key_data["expires_at"] is not None

    rotated_key = await client.post(
        f"/v1/projects/{project_id}/api-keys/{key_data['public_id']}/rotate",
        headers=headers,
        json={"overlap_seconds": 10},
    )
    assert rotated_key.status_code == 201, rotated_key.text
    rotated_data = rotated_key.json()
    assert (
        rotated_data["rotation_family_id"] == (key_data["rotation_family_id"])
    )
    old_key = await db_session.scalar(
        select(ApiKey).where(ApiKey.public_id == key_data["public_id"])
    )
    assert old_key is not None and old_key.is_active
    assert old_key.expires_at is not None
    old_expiry = old_key.expires_at
    if old_expiry.tzinfo is None:
        old_expiry = old_expiry.replace(tzinfo=timezone.utc)
    assert old_expiry <= datetime.now(timezone.utc) + timedelta(seconds=15)

    endpoint = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=headers,
        json={"url": "https://receiver.example/phase7"},
    )
    assert endpoint.status_code == 201, endpoint.text
    endpoint_id = endpoint.json()["public_id"]
    rotation = await client.post(
        f"/v1/projects/{project_id}/endpoints/{endpoint_id}/rotate-secret",
        headers=headers,
    )
    assert rotation.status_code == 200, rotation.text
    versions = list(
        await db_session.scalars(
            select(EndpointSigningSecretVersion).order_by(
                EndpointSigningSecretVersion.version
            )
        )
    )
    assert [version.version for version in versions] == [1, 2]
    assert versions[0].retire_at is not None
    assert versions[1].retire_at is None
    previous_valid_until = datetime.fromisoformat(
        rotation.json()["previous_valid_until"]
    )
    old_retire_at = versions[0].retire_at
    if old_retire_at.tzinfo is None:
        old_retire_at = old_retire_at.replace(tzinfo=timezone.utc)
    assert previous_valid_until == old_retire_at

    accepted = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": rotated_data["plaintext_key"],
            "Idempotency-Key": "phase7-replay-event",
        },
        json={"type": "phase7.test", "payload": {"safe": True}},
    )
    assert accepted.status_code == 202, accepted.text
    delivery = await db_session.scalar(select(Delivery))
    event = await db_session.scalar(select(Event))
    assert delivery is not None and event is not None
    delivery.status = "dead"
    delivery.dead_at = datetime.now(timezone.utc)
    event.canonical_envelope = b"{}"
    event.payload = None
    event.payload_purged_at = None
    await db_session.commit()

    replay = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=headers,
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "CONFLICT"

    event.payload = {}
    event.canonical_envelope = None
    await db_session.commit()
    missing_envelope = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=headers,
    )
    assert missing_envelope.status_code == 409

    event.canonical_envelope = b"{}"
    event.payload_purged_at = datetime.now(timezone.utc)
    await db_session.commit()
    purge_marked = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=headers,
    )
    assert purge_marked.status_code == 409

    event.payload_purged_at = None
    delivery.signing_secret_version_snapshot = 999
    await db_session.commit()
    missing_version = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=headers,
    )
    assert missing_version.status_code == 409
    assert "version" in missing_version.json()["error"]["message"]

    delivery.signing_secret_version_snapshot = 1
    versions[0].retire_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    retired_replay = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/replay",
        headers=headers,
    )
    assert retired_replay.status_code == 409
    assert "version" in retired_replay.json()["error"]["message"]

    revoked = await client.delete(
        f"/v1/projects/{project_id}/api-keys/{rotated_data['public_id']}",
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    rejected_after_revoke = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": rotated_data["plaintext_key"],
            "Idempotency-Key": "phase7-revoked-event",
        },
        json={"type": "phase7.revoked", "payload": {}},
    )
    assert rejected_after_revoke.status_code == 401


@pytest.mark.asyncio
async def test_policy_audit_export_purge_and_deletion_lifecycle(
    client: httpx.AsyncClient,
    auth,
    other_auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = worker_settings().model_copy(
        update={"delivery_retention_days": 1}
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, headers = auth
    other_user, other_headers = other_auth
    organization_id, project_id = await create_tenant(client, headers)
    member = await client.post(
        f"/v1/organizations/{organization_id}/members",
        headers=headers,
        json={"user_id": other_user["id"], "role": "member"},
    )
    assert member.status_code == 201, member.text
    project_operator = await client.put(
        f"/v1/projects/{project_id}/members/{other_user['id']}",
        headers=headers,
        json={"role": "operator"},
    )
    assert project_operator.status_code == 200, project_operator.text

    policy = await client.patch(
        f"/v1/organizations/{organization_id}/policy",
        headers=headers,
        json={
            "plan": "standard",
            "payload_retention_days": 14,
            "response_retention_days": 7,
        },
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["plan"] == "standard"

    key = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=headers,
        json={"name": "producer"},
    )
    endpoint = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=headers,
        json={"url": "https://receiver.example/audit"},
    )
    assert key.status_code == endpoint.status_code == 201
    accepted = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": key.json()["plaintext_key"],
            "Idempotency-Key": "phase7-cancel-event",
        },
        json={"type": "phase7.cancel", "payload": {}},
    )
    assert accepted.status_code == 202
    delivery = await db_session.scalar(select(Delivery))
    assert delivery is not None
    canceled = await client.post(
        f"/v1/projects/{project_id}/deliveries/{delivery.public_id}/cancel",
        headers=headers,
        json={"reason": "operator request"},
    )
    assert canceled.status_code == 200, canceled.text
    delivery.updated_at = datetime.now(timezone.utc) - timedelta(days=2)
    await db_session.commit()
    purged = await client.post(
        f"/v1/projects/{project_id}/deliveries/purge",
        headers=headers,
        json={"dry_run": False, "max_records": 10},
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged"] == 1

    denied_deactivation = await client.delete(
        f"/v1/projects/{project_id}/endpoints/{endpoint.json()['public_id']}",
        headers=other_headers,
    )
    assert denied_deactivation.status_code == 403
    deactivated = await client.delete(
        f"/v1/projects/{project_id}/endpoints/{endpoint.json()['public_id']}",
        headers=headers,
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["is_active"] is False
    deactivated_again = await client.delete(
        f"/v1/projects/{project_id}/endpoints/{endpoint.json()['public_id']}",
        headers=headers,
    )
    assert deactivated_again.status_code == 200, deactivated_again.text
    assert deactivated_again.json()["is_active"] is False

    exported = await client.get(
        f"/v1/organizations/{organization_id}/export",
        headers=headers,
        params={"limit": 100},
    )
    assert exported.status_code == 200, exported.text
    export_data = exported.json()
    assert export_data["policy"]["plan"] == "standard"
    assert export_data["counts"] == {
        "members": 2,
        "projects": 1,
        "project_members": 2,
        "api_keys": 1,
        "endpoints": 1,
        "events": 1,
        "deliveries": 0,
        "attempts": 0,
    }
    assert "plaintext_key" not in exported.text
    assert "signing_secret" not in exported.text
    assert "receiver.example" not in exported.text
    assert export_data["api_keys"][0]["prefix"] == key.json()["key_prefix"]
    assert export_data["api_keys"][0]["status"] == "active"
    assert "url" not in export_data["endpoints"][0]
    assert "signing_secret" not in export_data["endpoints"][0]

    bounded_export = await client.get(
        f"/v1/organizations/{organization_id}/export",
        headers=headers,
        params={"limit": 1},
    )
    assert bounded_export.status_code == 200, bounded_export.text
    bounded_data = bounded_export.json()
    assert bounded_data["truncated"] is True
    assert len(bounded_data["members"]) <= 1
    assert len(bounded_data["projects"]) <= 1
    assert len(bounded_data["audit_events"]) <= 1
    assert all(value <= 1 for value in bounded_data["counts"].values())

    audit = await client.get(
        f"/v1/organizations/{organization_id}/audit-events",
        headers=headers,
        params={"limit": 100},
    )
    assert audit.status_code == 200, audit.text
    actions = {event["action"] for event in audit.json()}
    assert {
        "organization.policy_updated",
        "api_key.created",
        "endpoint.created",
        "endpoint.deactivated",
        "delivery.canceled",
        "delivery.purged",
    } <= actions
    latest_audit = audit.json()[0]
    older_audit = await client.get(
        f"/v1/organizations/{organization_id}/audit-events",
        headers=headers,
        params={
            "before_created_at": latest_audit["created_at"],
            "before_id": latest_audit["id"],
            "limit": 100,
        },
    )
    assert older_audit.status_code == 200, older_audit.text
    assert all(
        event["public_id"] != latest_audit["public_id"]
        for event in older_audit.json()
    )

    forbidden = (
        "secret",
        "token",
        "key",
        "digest",
        "payload",
        "body",
        "url",
        "authorization",
    )
    events = list(await db_session.scalars(select(AdministrativeAuditEvent)))
    for event in events:
        for metadata_key in event.sanitized_metadata:
            normalized = "".join(
                character
                for character in metadata_key.lower()
                if character.isalnum()
            )
            assert not any(part in normalized for part in forbidden)

    requested = await client.delete(
        f"/v1/organizations/{organization_id}", headers=headers
    )
    assert requested.status_code == 202, requested.text
    assert requested.json()["status"] == "pending"
    repeated_request = await client.delete(
        f"/v1/organizations/{organization_id}", headers=headers
    )
    assert repeated_request.status_code == 202, repeated_request.text
    assert (
        repeated_request.json()["public_id"] == requested.json()["public_id"]
    )
    owner_status = await client.get(
        f"/v1/organizations/{organization_id}/deletion", headers=headers
    )
    assert owner_status.status_code == 200, owner_status.text
    denied_status = await client.get(
        f"/v1/organizations/{organization_id}/deletion",
        headers=other_headers,
    )
    assert denied_status.status_code == 200
    blocked = await client.patch(
        f"/v1/organizations/{organization_id}/policy",
        headers=headers,
        json={"plan": "enterprise"},
    )
    assert blocked.status_code == 403
    blocked_ingestion = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": key.json()["plaintext_key"],
            "Idempotency-Key": "pending",
        },
        json={"type": "phase7.pending", "payload": {}},
    )
    assert blocked_ingestion.status_code == 401
    canceled_deletion = await client.post(
        f"/v1/organizations/{organization_id}/deletion/cancel",
        headers=headers,
    )
    assert canceled_deletion.status_code == 200, canceled_deletion.text
    assert canceled_deletion.json()["status"] == "canceled"
    active_policy = await client.patch(
        f"/v1/organizations/{organization_id}/policy",
        headers=headers,
        json={"plan": "enterprise"},
    )
    assert active_policy.status_code == 200
    restored_ingestion = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": key.json()["plaintext_key"],
            "Idempotency-Key": "restored",
        },
        json={"type": "phase7.restored", "payload": {}},
    )
    assert restored_ingestion.status_code == 202, restored_ingestion.text


@pytest.mark.asyncio
async def test_worker_lifecycle_retention_and_bounded_deletion(
    client: httpx.AsyncClient,
    auth,
    db_session: AsyncSession,
    sqlite_session_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.lifecycle import LifecycleService
    from app.models import (
        AdministrativeAuditEvent,
        DeliveryAttempt,
        Organization,
        OrganizationLifecycleOperation,
    )

    settings = worker_settings().model_copy(
        update={
            "lifecycle_cleanup_batch_size": 10,
            "organization_deletion_grace_hours": 1,
        }
    )
    monkeypatch.setattr(factory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, headers = auth
    organization_id, project_id = await create_tenant(client, headers)
    api_key = await client.post(
        f"/v1/projects/{project_id}/api-keys",
        headers=headers,
        json={"name": "retention"},
    )
    endpoint = await client.post(
        f"/v1/projects/{project_id}/endpoints",
        headers=headers,
        json={"url": "https://receiver.example/retention"},
    )
    assert api_key.status_code == endpoint.status_code == 201
    accepted = await client.post(
        "/v1/events",
        headers={
            "X-API-Key": api_key.json()["plaintext_key"],
            "Idempotency-Key": "phase7-retention-event",
        },
        json={"type": "phase7.retention", "payload": {"safe": True}},
    )
    assert accepted.status_code == 202, accepted.text

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)
    event = await db_session.scalar(select(Event))
    delivery = await db_session.scalar(select(Delivery))
    policy = await db_session.scalar(select(OrganizationPolicy))
    assert event is not None and delivery is not None and policy is not None
    policy.payload_retention_days = 1
    policy.response_retention_days = 1
    event.created_at = old
    delivery.status = "succeeded"
    delivery.succeeded_at = old
    delivery.updated_at = old
    db_session.add(
        DeliveryAttempt(
            delivery_id=delivery.id,
            attempt_number=1,
            started_at=old,
            finished_at=old,
            outcome="succeeded",
            http_status=200,
            response_body="bounded response",
        )
    )
    await db_session.commit()

    lifecycle = LifecycleService(sqlite_session_factory, settings)
    retained = await lifecycle.run_once()
    assert retained.responses_purged == 1
    assert retained.payloads_purged == 1
    async with sqlite_session_factory() as session:
        stored_event = await session.get(Event, event.id)
        attempt = await session.scalar(select(DeliveryAttempt))
        assert stored_event is not None and attempt is not None
        assert stored_event.payload is None
        assert stored_event.canonical_envelope is None
        assert stored_event.payload_purged_at is not None
        assert attempt.response_body is None
        assert attempt.response_purged_at is not None

    requested = await client.delete(
        f"/v1/organizations/{organization_id}", headers=headers
    )
    assert requested.status_code == 202, requested.text
    operation_id = requested.json()["public_id"]
    operation = await db_session.scalar(
        select(OrganizationLifecycleOperation).where(
            OrganizationLifecycleOperation.public_id == operation_id
        )
    )
    assert operation is not None
    operation.scheduled_at = now - timedelta(seconds=1)
    await db_session.commit()

    aggregate_deleted = 0
    for _ in range(4):
        result = await lifecycle.run_once()
        aggregate_deleted += result.organizations_deleted
        if aggregate_deleted:
            break
    assert aggregate_deleted == 1
    async with sqlite_session_factory() as session:
        organization = await session.scalar(
            select(Organization).where(
                Organization.public_id == organization_id
            )
        )
        deletion_audit = await session.scalar(
            select(AdministrativeAuditEvent).where(
                AdministrativeAuditEvent.organization_public_id
                == organization_id,
                AdministrativeAuditEvent.action == "organization.deleted",
            )
        )
        assert organization is None
        assert deletion_audit is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_audit_history_rejects_update_and_delete(
    postgres_session_factory,
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.audit import append_audit

    async with postgres_session_factory() as session:
        event = await append_audit(
            session,
            1,
            "00000000-0000-0000-0000-000000000007",
            "phase7.immutability_verified",
            "verification",
            metadata={"safe": True},
        )
        await session.commit()
        public_id = event.public_id

    async with postgres_session_factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE administrative_audit_events "
                    "SET action = 'changed' WHERE public_id = :public_id"
                ),
                {"public_id": public_id},
            )
        await session.rollback()

    async with postgres_session_factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "DELETE FROM administrative_audit_events "
                    "WHERE public_id = :public_id"
                ),
                {"public_id": public_id},
            )
        await session.rollback()
