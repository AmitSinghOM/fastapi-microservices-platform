import hashlib
import hmac
import json

import pytest

from webhook_platform_sdk.receiver import (
    DuplicateEvent,
    InvalidSignature,
    PayloadTooLarge,
    verify_and_claim,
    verify_request,
)


class Deduplicator:
    def __init__(self) -> None:
        self.ids: set[str] = set()

    def claim(self, event_id: str) -> bool:
        if event_id in self.ids:
            return False
        self.ids.add(event_id)
        return True


def signed_request(secret: str, timestamp: int = 1_700_000_000):
    body = json.dumps(
        {"id": "event-1", "type": "order.created", "data": {}},
        separators=(",", ":"),
    ).encode()
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Webhook-Id": "event-1",
        "Webhook-Event": "order.created",
        "Webhook-Timestamp": str(timestamp),
        "Webhook-Signature": f"t={timestamp},v1={digest}",
    }
    return body, headers


def test_verifies_raw_bytes_before_parsing_and_claims_once() -> None:
    body, headers = signed_request("receiver-secret")
    deduplicator = Deduplicator()
    event = verify_and_claim(
        body, headers, "receiver-secret", deduplicator, now=1_700_000_010
    )
    assert event.event_id == "event-1"
    with pytest.raises(DuplicateEvent):
        verify_and_claim(
            body, headers, "receiver-secret", deduplicator, now=1_700_000_010
        )


def test_rejects_changed_raw_bytes() -> None:
    body, headers = signed_request("receiver-secret")
    with pytest.raises(InvalidSignature):
        verify_request(
            body + b" ", headers, "receiver-secret", now=1_700_000_010
        )


def test_oversized_body_is_rejected_before_hmac_or_parsing() -> None:
    """The size guard runs even for a body with no valid signature at all,
    since it is checked before HMAC verification, not after."""
    body, headers = signed_request("receiver-secret")
    with pytest.raises(PayloadTooLarge) as excinfo:
        verify_request(
            body,
            headers,
            "wrong-secret",
            now=1_700_000_010,
            max_body_bytes=len(body) - 1,
        )
    assert excinfo.value.size == len(body)
    assert excinfo.value.limit == len(body) - 1


def test_max_body_bytes_none_disables_the_guard() -> None:
    body, headers = signed_request("receiver-secret")
    event = verify_request(
        body,
        headers,
        "receiver-secret",
        now=1_700_000_010,
        max_body_bytes=None,
    )
    assert event.event_id == "event-1"


def test_body_within_the_limit_is_unaffected() -> None:
    body, headers = signed_request("receiver-secret")
    event = verify_request(
        body,
        headers,
        "receiver-secret",
        now=1_700_000_010,
        max_body_bytes=len(body),
    )
    assert event.event_id == "event-1"
