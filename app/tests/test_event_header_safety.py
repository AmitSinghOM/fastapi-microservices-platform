"""Event-type header safety: schema rejection, plus worker-side
LocalProtocolError (terminal) vs RemoteProtocolError (retries) finalization.
"""

from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.services.delivery_service as delivery_module
from app.models import Delivery, DeliveryAttempt
from app.schemas.webhooks import (
    EventCreate,
    validate_event_type,
    validate_event_type_filters,
)
from app.services.delivery_service import DeliveryService
from app.tests.test_delivery_contracts import (
    now,
    seed_delivery,
    worker_settings,
)
from app.tests.test_phase4_admission import api_project_key
from app.tests.test_worker_responses import ReceiverStream


# ---------------------------------------------------------------------------
# Schema rejection
# ---------------------------------------------------------------------------

UNSAFE_EVENT_TYPES = [
    "order\x00created",  # NUL
    "order\x01created",  # other control char
    "order\r\ncreated",  # CRLF (header injection shape)
    "order\tcreated",  # tab
    "ordér.created",  # non-ASCII
    "café.created",  # non-ASCII
    " order.created",  # leading whitespace
    "order.created ",  # trailing whitespace
]


@pytest.mark.parametrize("bad", UNSAFE_EVENT_TYPES)
def test_validate_event_type_rejects_unsafe_values(bad):
    with pytest.raises(ValueError):
        validate_event_type(bad)


def test_validate_event_type_accepts_printable_ascii():
    assert validate_event_type("order.created") == "order.created"
    assert validate_event_type("Order-Created_v1.0") == "Order-Created_v1.0"


@pytest.mark.parametrize("bad", UNSAFE_EVENT_TYPES)
def test_event_create_schema_rejects_unsafe_type(bad):
    with pytest.raises(ValidationError):
        EventCreate(type=bad, payload={})


@pytest.mark.parametrize(
    "bad",
    [
        "order\x00created",
        "order\x01.*",
        "ordér.*",
        "café.created",
    ],
)
def test_event_type_filters_reject_unsafe_characters(bad):
    with pytest.raises(ValueError):
        validate_event_type_filters([bad])


def test_event_type_filters_still_accept_printable_ascii():
    assert validate_event_type_filters(["order.*", "user.created"]) == [
        "order.*",
        "user.created",
    ]


@pytest.mark.asyncio
async def test_ingest_event_api_rejects_unsafe_type(client, auth):
    _, bearer = auth
    _, key = await api_project_key(client, bearer)

    for bad in UNSAFE_EVENT_TYPES:
        response = await client.post(
            "/v1/events",
            headers={"X-API-Key": key, "Idempotency-Key": f"hdr-{hash(bad)}"},
            json={"type": bad, "payload": {}},
        )
        assert response.status_code == 422, (bad, response.text)


# ---------------------------------------------------------------------------
# Worker: LocalProtocolError is terminal and recorded; RemoteProtocolError
# retries as before.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def local_target(monkeypatch):
    async def allow(*args):
        return args[0]

    monkeypatch.setattr(delivery_module, "validate_webhook_url", allow)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_headers", [False, True])
async def test_local_protocol_error_finalizes_terminally_no_reclaim(
    db_session, sqlite_session_factory, after_headers
):
    """Terminal on attempt 1: dead, recorded, lease released -- not stuck
    for lease-expiry reclaim."""
    factory = sqlite_session_factory
    delivery_id = await seed_delivery(factory)
    settings = worker_settings().model_copy(update={"webhook_max_attempts": 5})

    def receiver(request):
        if not after_headers:
            raise httpx.LocalProtocolError("unsendable request")
        return httpx.Response(
            200, stream=ReceiverStream(error=httpx.LocalProtocolError)
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(factory, client, settings)
        claim = (await service.claim_due(1))[0]
        assert claim.attempt_number == 1
        finalized = await service.deliver(claim)

    assert finalized

    async with factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).order_by(DeliveryAttempt.id)
            )
        )
        # Terminal on the first attempt: not scheduled for retry despite
        # max_attempts allowing more.
        assert delivery.status == "dead"
        assert delivery.dead_reason == "LocalProtocolError"
        assert delivery.lease_token is None
        assert delivery.lease_expires_at is None
        assert len(attempts) == 1
        assert attempts[0].error == "LocalProtocolError"
        assert attempts[0].outcome == "dead"

    # No reclaim: a second claim pass finds nothing due (dead is terminal,
    # not retry_scheduled, so there is nothing left to reclaim either).
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(factory, client, settings)
        assert await service.claim_due(1) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("after_headers", [False, True])
async def test_remote_protocol_error_still_retries(
    db_session, sqlite_session_factory, after_headers
):
    """Unchanged: RemoteProtocolError stays retryable."""
    factory = sqlite_session_factory
    delivery_id = await seed_delivery(factory)
    settings = worker_settings().model_copy(update={"webhook_max_attempts": 2})

    def receiver(request):
        if not after_headers:
            raise httpx.RemoteProtocolError("receiver misbehaved")
        return httpx.Response(
            200, stream=ReceiverStream(error=httpx.RemoteProtocolError)
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(factory, client, settings)

        claim = (await service.claim_due(1))[0]
        assert claim.attempt_number == 1
        assert await service.deliver(claim)
        async with factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            assert delivery.status == "retry_scheduled"
            delivery.next_attempt_at = now() - timedelta(seconds=1)
            await session.commit()

        claim = (await service.claim_due(1))[0]
        assert claim.attempt_number == 2
        assert await service.deliver(claim)

    async with factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).order_by(DeliveryAttempt.id)
            )
        )
        assert delivery.status == "dead"
        assert delivery.dead_reason == "max_attempts"
        assert len(attempts) == 2
        assert all(a.error == "RemoteProtocolError" for a in attempts)
