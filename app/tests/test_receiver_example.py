import asyncio
import importlib.util
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app.webhook_security import (
    canonical_json,
    endpoint_secret,
    endpoint_secret_digest,
    sign_payload,
    sign_payload_standard,
)

ROOT = Path(__file__).resolve().parents[2]
SECRET = endpoint_secret("receiver-test-key", "endpoint-1", 1)
BODY = canonical_json(
    {"id": "event-1", "type": "order.created", "data": {"order_id": "42"}}
)


def load_receiver(
    monkeypatch,
    tmp_path,
    token,
    *,
    max_body_bytes=None,
    body_timeout_seconds=None,
):
    monkeypatch.syspath_prepend(str(ROOT / "sdk/python/src"))
    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("RECEIVER_DATABASE", str(tmp_path / "inbox.db"))
    if token is None:
        monkeypatch.delenv("WEBHOOK_RECEIVER_BEARER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WEBHOOK_RECEIVER_BEARER_TOKEN", token)
    if max_body_bytes is None:
        monkeypatch.delenv("WEBHOOK_RECEIVER_MAX_BODY_BYTES", raising=False)
    else:
        monkeypatch.setenv(
            "WEBHOOK_RECEIVER_MAX_BODY_BYTES", str(max_body_bytes)
        )
    if body_timeout_seconds is None:
        monkeypatch.delenv("RECEIVER_BODY_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv(
            "RECEIVER_BODY_TIMEOUT_SECONDS", str(body_timeout_seconds)
        )
    spec = importlib.util.spec_from_file_location(
        "receiver_example", ROOT / "examples/receiver/app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signed_headers(scheme):
    if scheme == "legacy":
        timestamp, signature = sign_payload(BODY, SECRET)
    else:
        timestamp, signature = sign_payload_standard(
            "event-1",
            BODY,
            endpoint_secret_digest("receiver-test-key", "endpoint-1", 1),
        )
    return {
        "webhook-id": "event-1",
        "webhook-event": "order.created",
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature,
    }


@pytest.mark.parametrize("scheme", ["legacy", "standard"])
@pytest.mark.parametrize("token", [None, "ingress-owned-token"])
async def test_receiver_requires_signature_and_durably_deduplicates(
    monkeypatch, tmp_path, scheme, token
):
    receiver = load_receiver(monkeypatch, tmp_path, token)
    headers = signed_headers(scheme)
    if token:
        headers["authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver.app),
        base_url="http://receiver.test",
    ) as client:
        invalid = await client.post(
            "/webhooks", content=BODY + b" ", headers=headers
        )
        assert invalid.status_code == 400
        assert not receiver.database_path.exists()
        first = await client.post("/webhooks", content=BODY, headers=headers)
        assert first.status_code == 202
        reloaded = load_receiver(monkeypatch, tmp_path, token)
        assert not reloaded.accept_once("event-1", "order.created", BODY)
        repeated = await client.post(
            "/webhooks", content=BODY, headers=headers
        )
        assert repeated.status_code == 204
    with sqlite3.connect(receiver.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM inbox").fetchone() == (
            1,
        )


@pytest.mark.parametrize(
    "authorization", [None, b"Bearer wrong", b"Basic token", b"Bearer \xff"]
)
async def test_receiver_bearer_rejection_does_not_accept_event(
    monkeypatch, tmp_path, authorization
):
    receiver = load_receiver(monkeypatch, tmp_path, "ingress-owned-token")
    headers = signed_headers("standard")
    if authorization is not None:
        headers["authorization"] = authorization
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver.app),
        base_url="http://receiver.test",
    ) as client:
        response = await client.post(
            "/webhooks", content=BODY, headers=headers
        )
    assert response.status_code == 401
    assert not receiver.database_path.exists()


def test_receiver_empty_bearer_configuration_fails_closed(
    monkeypatch, tmp_path
):
    with pytest.raises(RuntimeError, match="must not be empty"):
        load_receiver(monkeypatch, tmp_path, "")


async def test_receiver_rejects_oversized_body_before_persisting(
    monkeypatch, tmp_path
):
    receiver = load_receiver(monkeypatch, tmp_path, None, max_body_bytes=16)
    headers = signed_headers("standard")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver.app),
        base_url="http://receiver.test",
    ) as client:
        response = await client.post(
            "/webhooks", content=BODY, headers=headers
        )
    assert response.status_code == 413
    assert not receiver.database_path.exists()


async def test_receiver_accepts_chunked_body_without_content_length(
    monkeypatch, tmp_path
):
    """A body streamed in small pieces, with no Content-Length header,
    must still be bounded-read and verified correctly (the receiver must
    not rely on a trustworthy Content-Length)."""
    receiver = load_receiver(monkeypatch, tmp_path, None)
    headers = signed_headers("standard")

    async def chunks():
        for start in range(0, len(BODY), 3):
            yield BODY[start : start + 3]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver.app),
        base_url="http://receiver.test",
    ) as client:
        response = await client.post(
            "/webhooks", content=chunks(), headers=headers
        )
    assert response.status_code == 202
    with sqlite3.connect(receiver.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM inbox").fetchone() == (
            1,
        )


async def test_receiver_times_out_a_stalled_body_read(monkeypatch, tmp_path):
    """A client that stops sending bytes mid-request (but stays under the
    byte cap) must not hold the read open forever; it is cut off by the
    total read timeout instead. The stall is a never-set ``asyncio.Event``,
    not a sleep, so the test only waits as long as the short timeout."""
    receiver = load_receiver(
        monkeypatch, tmp_path, None, body_timeout_seconds=0.05
    )
    headers = signed_headers("standard")
    stall = asyncio.Event()

    async def never_finishes():
        yield BODY[:4]
        await stall.wait()
        yield BODY[4:]  # pragma: no cover - never reached

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver.app),
        base_url="http://receiver.test",
    ) as client:
        response = await client.post(
            "/webhooks", content=never_finishes(), headers=headers
        )
    assert response.status_code == 408
    assert not receiver.database_path.exists()


async def test_bounded_body_read_stops_after_first_chunk_over_limit(
    monkeypatch, tmp_path
):
    """Overflow must be detected on the chunk that crosses the limit, and
    the read loop must not pull a further chunk afterward."""
    receiver = load_receiver(monkeypatch, tmp_path, None, max_body_bytes=8)

    class StreamingRequest:
        async def stream(self):
            yield b"0123456789"  # 10 bytes, already over the 8-byte limit
            pytest.fail("read a chunk after the byte cap was exceeded")
            yield b"unreachable"  # pragma: no cover

    with pytest.raises(HTTPException) as excinfo:
        await receiver._read_bounded_body(StreamingRequest(), 8)
    assert excinfo.value.status_code == 413


@pytest.mark.parametrize(
    ("limit_delta", "expected_status"),
    [(0, 202), (-1, 413)],
)
async def test_body_size_exact_boundary(
    monkeypatch, tmp_path, limit_delta, expected_status
):
    """A body exactly at the configured limit is accepted; one byte over
    (the limit set one byte below the body's exact size) is rejected."""
    receiver = load_receiver(
        monkeypatch,
        tmp_path,
        None,
        max_body_bytes=len(BODY) + limit_delta,
    )
    headers = signed_headers("standard")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receiver.app),
        base_url="http://receiver.test",
    ) as client:
        response = await client.post(
            "/webhooks", content=BODY, headers=headers
        )
    assert response.status_code == expected_status
    assert receiver.database_path.exists() == (expected_status == 202)
