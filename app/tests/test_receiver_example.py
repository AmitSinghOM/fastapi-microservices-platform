import importlib.util
import sqlite3
from pathlib import Path

import httpx
import pytest

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


def load_receiver(monkeypatch, tmp_path, token):
    monkeypatch.syspath_prepend(str(ROOT / "sdk/python/src"))
    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("RECEIVER_DATABASE", str(tmp_path / "inbox.db"))
    if token is None:
        monkeypatch.delenv("WEBHOOK_RECEIVER_BEARER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WEBHOOK_RECEIVER_BEARER_TOKEN", token)
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
