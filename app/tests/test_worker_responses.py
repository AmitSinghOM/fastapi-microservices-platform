import asyncio
import gzip
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

import app.services.delivery_service as delivery_module
from app.models import Delivery, DeliveryAttempt, EndpointSigningSecretVersion
from app.services.delivery_service import DeliveryService
from app.services.webhook_service import WebhookService
from app.tests.test_delivery_contracts import (
    now,
    seed_delivery,
    worker_settings,
)
from app.tests.test_phase2_safety import fake_claim


class ReceiverStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), error=None):
        self.chunks = chunks
        self.error = error
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error:
            raise self.error("receiver-controlled secret")

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def local_target(monkeypatch):
    async def allow_target(*args):
        return args[0]

    monkeypatch.setattr(delivery_module, "validate_webhook_url", allow_target)


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, 16])
async def test_response_capture_stops_at_cap_without_reading_next_chunk(cap):
    stream = ReceiverStream([b"a" * 64, b"not read"])
    settings = worker_settings().model_copy(
        update={"webhook_response_max_bytes": cap}
    )

    def receiver(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        result = await DeliveryService(
            None, client, settings
        )._perform_attempt(fake_claim(1))
    assert result.succeeded
    assert result.response_body == "a" * cap
    assert stream.reads == (1 if cap else 0)
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [gzip.compress(b"a" * 1_000_000), b"bad gzip"]
)
async def test_compressed_response_is_never_decoded(body):
    stream = ReceiverStream([body])

    def receiver(request):
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=stream
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        result = await DeliveryService(
            None, client, worker_settings()
        )._perform_attempt(fake_claim(1))
    assert result.succeeded
    assert result.response_body == body.decode("utf-8", errors="replace")
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [httpx.RemoteProtocolError, httpx.DecodingError, httpx.ReadTimeout],
)
@pytest.mark.parametrize("after_headers", [False, True])
@pytest.mark.parametrize("max_attempts", [1, 2])
async def test_receiver_failures_finalize_with_bounded_retry(
    db_session, sqlite_session_factory, error, after_headers, max_attempts
):
    factory = sqlite_session_factory
    delivery_id = await seed_delivery(factory)
    settings = worker_settings().model_copy(
        update={"webhook_max_attempts": max_attempts}
    )

    def receiver(request):
        if not after_headers:
            raise error("receiver-controlled secret")
        return httpx.Response(200, stream=ReceiverStream(error=error))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(factory, client, settings)
        for attempt_number in range(1, max_attempts + 1):
            claim = (await service.claim_due(1))[0]
            assert claim.attempt_number == attempt_number
            assert await service.deliver(claim)
            async with factory() as session:
                delivery = await session.get(Delivery, delivery_id)
                if attempt_number < max_attempts:
                    assert delivery.status == "retry_scheduled"
                    assert delivery.next_attempt_at is not None
                    delivery.next_attempt_at = now() - timedelta(seconds=1)
                    await session.commit()
    async with factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        attempts = list(
            await session.scalars(
                select(DeliveryAttempt).order_by(DeliveryAttempt.id)
            )
        )
        assert delivery.status == "dead"
        assert delivery.dead_reason == "max_attempts"
        assert len(attempts) == max_attempts
        assert all(attempt.error == error.__name__ for attempt in attempts)
        assert attempts[-1].outcome == "dead"
        assert delivery.lease_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("retired", [False, True])
async def test_replay_checks_signing_retirement_at_dispatch(
    db_session, sqlite_session_factory, retired
):
    factory = sqlite_session_factory
    delivery_id = await seed_delivery(factory)
    async with factory() as session:
        original = await session.get(Delivery, delivery_id)
        original.status = "dead"
        version = await session.scalar(select(EndpointSigningSecretVersion))
        version.retire_at = now() + timedelta(minutes=1)
        await WebhookService(session, worker_settings())._ensure_replayable(
            original, now()
        )
        replay = WebhookService._replay_copy(original, now())
        session.add(replay)
        await session.commit()
        replay_id = replay.id
        if retired:
            version.retire_at = now() - timedelta(seconds=1)
            await session.commit()
    requests = []

    def receiver(request):
        requests.append(request)
        return httpx.Response(204, stream=ReceiverStream())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(factory, client, worker_settings())
        claim = (await service.claim_due(1))[0]
        assert claim.id == replay_id
        assert await service.deliver(claim)
    assert len(requests) == (0 if retired else 1)
    async with factory() as session:
        stored = await session.get(Delivery, replay_id)
        assert stored.status == ("dead" if retired else "succeeded")
        if retired:
            assert stored.dead_reason == "signing_version_retired"


@pytest.mark.asyncio
async def test_elapsed_retirement_deadline_stops_before_send():
    claim = replace(
        fake_claim(1),
        signing_secret_deadline=asyncio.get_running_loop().time(),
    )

    def receiver(request):
        pytest.fail("retired signing version dispatched")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        result = await DeliveryService(
            None, client, worker_settings()
        )._perform_attempt(claim)
    assert not result.succeeded
    assert result.error == "signing_version_retired"


@pytest.mark.asyncio
async def test_missing_signing_version_fails_closed(
    db_session, sqlite_session_factory
):
    factory = sqlite_session_factory
    delivery_id = await seed_delivery(factory)
    async with factory() as session:
        version = await session.scalar(select(EndpointSigningSecretVersion))
        await session.delete(version)
        await session.commit()

    def receiver(request):
        pytest.fail("missing signing version dispatched")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receiver)
    ) as client:
        service = DeliveryService(factory, client, worker_settings())
        assert await service.deliver((await service.claim_due(1))[0])
    async with factory() as session:
        stored = await session.get(Delivery, delivery_id)
        assert stored.dead_reason == "signing_version_unavailable"


@pytest.mark.asyncio
async def test_signing_lookup_is_inside_overall_attempt_deadline():
    class BlockedLookup(DeliveryService):
        @asynccontextmanager
        async def _session(self):
            await asyncio.Event().wait()
            yield

    settings = worker_settings().model_copy(
        update={"worker_attempt_timeout_seconds": 0.01}
    )
    result = await BlockedLookup(None, None, settings)._bounded_attempt(
        fake_claim(1)
    )
    assert result.error == "attempt_timeout"
    assert result.retryable
    assert not result.succeeded
