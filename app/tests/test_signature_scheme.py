"""ADR 0002 phase one: per-endpoint signature schemes.

Covers the two secret serializations, frozen golden vectors for both
schemes, API behavior (creation, one-time secret on scheme upgrade,
delivery snapshots), spec-exact worker emission, and cross-verification of
``standard`` output with the official ``standardwebhooks`` library.
"""

import base64
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from standardwebhooks.webhooks import Webhook

import app.services.delivery_service as delivery_module
import app.services.webhook_service as webhook_module
from app.models import Delivery
from app.services.delivery_service import ClaimedDelivery, DeliveryService
from app.tests.test_delivery_contracts import worker_settings
from app.tests.test_phase4_admission import allow_target, api_project_key
from app.webhook_security import (
    endpoint_secret,
    endpoint_secret_digest,
    endpoint_secret_standard,
    sign_payload,
    sign_payload_standard,
)

SIGNING_KEY = "w" * 32
ENDPOINT_ID = "11111111-2222-3333-4444-555555555555"
BODY = (
    b'{"created_at":"2026-01-01T00:00:00+00:00","data":{"n":1},'
    b'"id":"ev-1","type":"order.created"}'
)


def test_secret_serializations_encode_same_digest():
    legacy = endpoint_secret(SIGNING_KEY, ENDPOINT_ID, 1)
    standard = endpoint_secret_standard(SIGNING_KEY, ENDPOINT_ID, 1)
    digest = endpoint_secret_digest(SIGNING_KEY, ENDPOINT_ID, 1)
    assert len(digest) == 32
    padded = legacy.removeprefix("whsec_") + "=="
    assert base64.urlsafe_b64decode(padded[: 44]) == digest
    assert base64.b64decode(standard.removeprefix("whsec_")) == digest
    assert legacy != standard


def test_frozen_golden_vectors():
    """Regression-lock both schemes byte for byte."""
    legacy_secret = endpoint_secret(SIGNING_KEY, ENDPOINT_ID, 1)
    assert legacy_secret == (
        "whsec_vTRXG2CwqFf6P80ez648Z8G7XaZ43olDednJZdwY4_Y"
    )
    assert sign_payload(BODY, legacy_secret, 1767225600) == (
        1767225600,
        "t=1767225600,"
        "v1=6b03e0ceb6090c37d1282ff13ddfed9448c36cdd0ad0dd1f5595fcfae95f450d",
    )
    assert endpoint_secret_standard(SIGNING_KEY, ENDPOINT_ID, 1) == (
        "whsec_vTRXG2CwqFf6P80ez648Z8G7XaZ43olDednJZdwY4/Y="
    )
    assert sign_payload_standard(
        "ev-1", BODY, endpoint_secret_digest(SIGNING_KEY, ENDPOINT_ID, 1),
        1767225600,
    ) == (1767225600, "v1,iH/jZ1lyFWRV9aCjqdIBDtxT1BlnAWLzUAmn+fLcBU0=")


def test_standard_signature_verified_by_official_library():
    """The acceptance gate: the spec's own library must verify our bytes."""
    now = int(time.time())
    timestamp, signature = sign_payload_standard(
        "ev-1", BODY, endpoint_secret_digest(SIGNING_KEY, ENDPOINT_ID, 1),
        now,
    )
    verifier = Webhook(endpoint_secret_standard(SIGNING_KEY, ENDPOINT_ID, 1))
    verified = verifier.verify(
        BODY.decode(),
        {
            "webhook-id": "ev-1",
            "webhook-timestamp": str(timestamp),
            "webhook-signature": signature,
        },
    )
    assert verified["type"] == "order.created"


async def _create_endpoint(client, bearer, project_id, scheme=None):
    body = {"url": f"https://receiver.example/{uuid4().hex}"}
    if scheme is not None:
        body["signature_scheme"] = scheme
    response = await client.post(
        f"/v1/projects/{project_id}/endpoints", headers=bearer, json=body
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_api_scheme_lifecycle_and_snapshots(
    client,
    auth,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(webhook_module, "validate_webhook_url", allow_target)
    _, bearer = auth
    project_id, key = await api_project_key(client, bearer)

    legacy = await _create_endpoint(client, bearer, project_id)
    assert legacy["signature_scheme"] == "legacy"
    assert "_" in legacy["signing_secret"] or "-" in legacy[
        "signing_secret"
    ] or not legacy["signing_secret"].endswith("=")

    standard = await _create_endpoint(
        client, bearer, project_id, scheme="standard"
    )
    assert standard["signature_scheme"] == "standard"
    # Standard serialization: padded standard base64 of 32 bytes.
    assert base64.b64decode(
        standard["signing_secret"].removeprefix("whsec_")
    ).__len__() == 32

    accepted = await client.post(
        "/v1/events",
        headers={"X-API-Key": key, "Idempotency-Key": "scheme-0001"},
        json={"type": "order.created", "payload": {"n": 1}},
    )
    assert accepted.status_code == 202, accepted.text
    snapshots = {
        row.endpoint_public_id_snapshot: row.signature_scheme_snapshot
        for row in await db_session.scalars(select(Delivery))
    }
    assert snapshots[legacy["public_id"]] == "legacy"
    assert snapshots[standard["public_id"]] == "standard"

    # Upgrading a legacy endpoint returns the standard-form secret once.
    upgraded = await client.patch(
        f"/v1/projects/{project_id}/endpoints/{legacy['public_id']}",
        headers=bearer,
        json={"signature_scheme": "standard"},
    )
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["signature_scheme"] == "standard"
    secret = upgraded.json()["signing_secret"]
    assert secret and secret.startswith("whsec_")
    assert base64.b64decode(secret.removeprefix("whsec_"))

    # Ordinary updates and standard->standard carry no secret.
    plain = await client.patch(
        f"/v1/projects/{project_id}/endpoints/{legacy['public_id']}",
        headers=bearer,
        json={"description": "no secret expected"},
    )
    assert plain.status_code == 200 and plain.json()["signing_secret"] is None

    # Accepted deliveries keep their snapshot after the endpoint changed.
    unchanged = {
        row.endpoint_public_id_snapshot: row.signature_scheme_snapshot
        for row in await db_session.scalars(select(Delivery))
    }
    assert unchanged[legacy["public_id"]] == "legacy"


@pytest.mark.asyncio
async def test_worker_emits_spec_exact_headers(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    del db_session
    captured: list[httpx.Request] = []

    async def allow_test_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    monkeypatch.setattr(
        delivery_module, "validate_webhook_url", allow_test_target
    )

    def receiver(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="accepted")

    settings = worker_settings()
    claim = ClaimedDelivery(
        id=1,
        public_id="dl-1",
        organization_id=1,
        endpoint_id=1,
        lease_token="lease",
        attempt_number=1,
        endpoint_public_id=ENDPOINT_ID,
        endpoint_url="https://receiver.example/hook",
        endpoint_secret_version=1,
        endpoint_active=True,
        event_public_id="ev-1",
        event_type="order.created",
        canonical_envelope=BODY,
        signature_scheme="standard",
        created_at=datetime.now(timezone.utc),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as http_client:
        service = DeliveryService(
            sqlite_session_factory, http_client, settings
        )
        result = await service._perform_attempt(claim)
    assert result.succeeded, result

    request = captured[0]
    assert request.headers["webhook-id"] == "ev-1"
    signature = request.headers["webhook-signature"]
    assert signature.startswith("v1,")
    verifier = Webhook(
        endpoint_secret_standard(
            settings.webhook_signing_key, ENDPOINT_ID, 1
        )
    )
    verified = verifier.verify(
        request.content.decode(),
        {
            "webhook-id": request.headers["webhook-id"],
            "webhook-timestamp": request.headers["webhook-timestamp"],
            "webhook-signature": signature,
        },
    )
    assert verified == json.loads(BODY)
    # Additive headers remain.
    assert request.headers["Webhook-Event"] == "order.created"
    assert request.headers["Webhook-Attempt"] == "1"


@pytest.mark.asyncio
async def test_worker_legacy_emission_unchanged(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    del db_session
    captured: list[httpx.Request] = []

    async def allow_test_target(url: str, allow_http: bool = False) -> str:
        del allow_http
        return url

    monkeypatch.setattr(
        delivery_module, "validate_webhook_url", allow_test_target
    )

    def receiver(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="accepted")

    settings = worker_settings()
    claim = ClaimedDelivery(
        id=1,
        public_id="dl-1",
        organization_id=1,
        endpoint_id=1,
        lease_token="lease",
        attempt_number=1,
        endpoint_public_id=ENDPOINT_ID,
        endpoint_url="https://receiver.example/hook",
        endpoint_secret_version=1,
        endpoint_active=True,
        event_public_id="ev-1",
        event_type="order.created",
        canonical_envelope=BODY,
        created_at=datetime.now(timezone.utc),
    )
    assert claim.signature_scheme == "legacy"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as http_client:
        service = DeliveryService(
            sqlite_session_factory, http_client, settings
        )
        result = await service._perform_attempt(claim)
    assert result.succeeded, result

    header = captured[0].headers["Webhook-Signature"]
    timestamp = int(captured[0].headers["Webhook-Timestamp"])
    secret = endpoint_secret(settings.webhook_signing_key, ENDPOINT_ID, 1)
    assert header == sign_payload(BODY, secret, timestamp)[1]
