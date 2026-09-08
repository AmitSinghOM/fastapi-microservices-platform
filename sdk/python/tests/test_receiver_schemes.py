"""Receiver auto-detection of legacy and Standard Webhooks signatures."""

import base64
import hashlib
import hmac
import time

import pytest

from webhook_platform_sdk.receiver import (
    InvalidSignature,
    SignatureExpired,
    verify_request,
    verify_signature,
    verify_signature_standard,
)

DIGEST = hashlib.sha256(b"test-key-material").digest()
LEGACY_SECRET = "whsec_" + base64.urlsafe_b64encode(DIGEST).decode().rstrip(
    "="
)
STANDARD_SECRET = "whsec_" + base64.b64encode(DIGEST).decode()
BODY = b'{"created_at":"2026-01-01T00:00:00+00:00","data":{"n":1},"id":"ev-1","type":"order.created"}'


def _legacy_headers(now: int) -> dict[str, str]:
    signed = str(now).encode() + b"." + BODY
    digest = hmac.new(
        LEGACY_SECRET.encode(), signed, hashlib.sha256
    ).hexdigest()
    return {
        "Webhook-Id": "ev-1",
        "Webhook-Event": "order.created",
        "Webhook-Timestamp": str(now),
        "Webhook-Signature": f"t={now},v1={digest}",
    }


def _standard_headers(now: int) -> dict[str, str]:
    signed = f"ev-1.{now}.".encode() + BODY
    signature = base64.b64encode(
        hmac.new(DIGEST, signed, hashlib.sha256).digest()
    ).decode()
    return {
        "webhook-id": "ev-1",
        "webhook-event": "order.created",
        "webhook-timestamp": str(now),
        "webhook-signature": f"v1,{signature}",
    }


def test_legacy_verification_unchanged():
    now = int(time.time())
    event = verify_request(BODY, _legacy_headers(now), LEGACY_SECRET, now=now)
    assert event.event_id == "ev-1" and event.timestamp == now


def test_standard_verification_with_standard_secret():
    now = int(time.time())
    event = verify_request(
        BODY, _standard_headers(now), STANDARD_SECRET, now=now
    )
    assert event.event_id == "ev-1" and event.timestamp == now


def test_standard_verification_accepts_legacy_serialized_secret():
    # Either serialization decodes to the same digest, so a receiver that
    # only holds the legacy string can still verify standard deliveries.
    now = int(time.time())
    event = verify_request(
        BODY, _standard_headers(now), LEGACY_SECRET, now=now
    )
    assert event.event_id == "ev-1"


def test_standard_rejects_tampered_body_and_id():
    now = int(time.time())
    headers = _standard_headers(now)
    with pytest.raises(InvalidSignature):
        verify_request(BODY + b" ", headers, STANDARD_SECRET, now=now)
    tampered = dict(headers, **{"webhook-id": "ev-2"})
    with pytest.raises(Exception):
        verify_request(BODY, tampered, STANDARD_SECRET, now=now)


def test_standard_rejects_stale_timestamp():
    now = int(time.time())
    with pytest.raises(SignatureExpired):
        verify_request(
            BODY,
            _standard_headers(now - 3_600),
            STANDARD_SECRET,
            now=now,
        )


def test_unknown_tokens_are_ignored_not_fatal():
    now = int(time.time())
    headers = _standard_headers(now)
    headers["webhook-signature"] = (
        "v1a,AAAA unknown "
        + headers["webhook-signature"]
    )
    event = verify_request(BODY, headers, STANDARD_SECRET, now=now)
    assert event.event_id == "ev-1"

    legacy = _legacy_headers(now)
    legacy["Webhook-Signature"] = (
        legacy["Webhook-Signature"] + " v1,AAAA"
    )
    event = verify_request(BODY, legacy, LEGACY_SECRET, now=now)
    assert event.event_id == "ev-1"


def test_standard_only_signature_functions():
    now = int(time.time())
    headers = _standard_headers(now)
    timestamp = verify_signature_standard(
        BODY,
        "ev-1",
        headers["webhook-timestamp"],
        headers["webhook-signature"],
        STANDARD_SECRET,
        now=now,
    )
    assert timestamp == now
    with pytest.raises(InvalidSignature):
        verify_signature(BODY, headers["webhook-signature"], LEGACY_SECRET)
    with pytest.raises(InvalidSignature):
        verify_signature_standard(
            BODY, "ev-1", "not-digits",
            headers["webhook-signature"], STANDARD_SECRET, now=now,
        )
