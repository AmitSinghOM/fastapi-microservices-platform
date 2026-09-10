"""Receiver auto-detection of legacy and Standard Webhooks signatures."""

import base64
import hashlib
import hmac
import time

import pytest

from webhook_platform_sdk.receiver import (
    InvalidSignature,
    SignatureExpired,
    _secret_key_bytes,
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


def _special_char_digest() -> bytes:
    """A digest whose base64 differs between the two serializations.

    Finds a digest containing ``+`` or ``/`` in standard base64 (and so
    ``-`` or ``_`` in base64url). Without this property the two secret
    forms are byte-identical and the decoder bug this guards against is
    invisible — which is exactly how it slipped past the original tests.
    """
    for i in range(1_000):
        digest = hashlib.sha256(f"probe-{i}".encode()).digest()
        encoded = base64.b64encode(digest).decode()
        if "+" in encoded or "/" in encoded:
            return digest
    raise AssertionError("no special-character digest found")


def test_either_secret_form_decodes_to_identical_key_bytes():
    """Regression: b64decode without validate=True silently dropped the
    urlsafe characters from a legacy-form secret and returned wrong bytes
    instead of falling through to the urlsafe decoder."""
    digest = _special_char_digest()
    legacy = "whsec_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
    standard = "whsec_" + base64.b64encode(digest).decode()
    assert legacy != standard  # the divergent case, by construction
    assert _secret_key_bytes(standard) == digest
    assert _secret_key_bytes(legacy) == digest


def test_standard_verification_with_special_char_legacy_secret():
    """End to end: a receiver holding only the legacy-form secret verifies
    a standard delivery even when the serializations diverge."""
    digest = _special_char_digest()
    legacy = "whsec_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
    now = int(time.time())
    signed = f"ev-1.{now}.".encode() + BODY
    signature = base64.b64encode(
        hmac.new(digest, signed, hashlib.sha256).digest()
    ).decode()
    headers = {
        "webhook-id": "ev-1",
        "webhook-event": "order.created",
        "webhook-timestamp": str(now),
        "webhook-signature": f"v1,{signature}",
    }
    event = verify_request(BODY, headers, legacy, now=now)
    assert event.event_id == "ev-1"


def test_non_ascii_signature_token_fails_closed_not_typeerror():
    """Regression: a non-ASCII v1 token reached hmac.compare_digest and
    raised TypeError (a 500 for receivers) instead of InvalidSignature."""
    now = int(time.time())
    headers = _standard_headers(now)
    only_bad = dict(
        headers, **{"webhook-signature": "v1,签名不对"}
    )
    with pytest.raises(InvalidSignature):
        verify_request(BODY, only_bad, STANDARD_SECRET, now=now)
    # A non-ASCII token alongside a valid one is ignored, not fatal.
    mixed = dict(
        headers,
        **{
            "webhook-signature": (
                "v1,签名不对 " + headers["webhook-signature"]
            )
        },
    )
    event = verify_request(BODY, mixed, STANDARD_SECRET, now=now)
    assert event.event_id == "ev-1"
