"""Raw-byte receiver verification and durable deduplication helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class ReceiverVerificationError(ValueError):
    """Base class for bounded receiver verification failures."""


class InvalidSignature(ReceiverVerificationError):
    def __init__(self) -> None:
        super().__init__("Webhook signature is invalid")


class SignatureExpired(ReceiverVerificationError):
    def __init__(self) -> None:
        super().__init__("Webhook timestamp is outside the allowed tolerance")


class DuplicateEvent(ReceiverVerificationError):
    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__("Webhook event was already accepted")


class DurableDeduplicator(Protocol):
    """Atomically reserve an event ID in durable receiver storage."""

    def claim(self, event_id: str) -> bool:
        """Return true only for the first durable reservation."""


@dataclass(frozen=True)
class ReceiverEvent:
    event_id: str
    event_type: str
    timestamp: int
    body: dict[str, Any]
    raw_body: bytes


def _signature_parts(header: str) -> tuple[int, list[str]]:
    timestamp: int | None = None
    signatures: list[str] = []
    for item in header.split(","):
        name, separator, value = item.strip().partition("=")
        if not separator or not value:
            raise InvalidSignature()
        if name == "t":
            if timestamp is not None or not value.isdigit():
                raise InvalidSignature()
            timestamp = int(value)
        elif name == "v1":
            if len(value) == 64:
                try:
                    bytes.fromhex(value)
                except ValueError as exc:
                    raise InvalidSignature() from exc
                signatures.append(value.lower())
    if timestamp is None or not signatures:
        raise InvalidSignature()
    return timestamp, signatures


def verify_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> int:
    """Verify HMAC over timestamp + period + exact unparsed body bytes."""
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be non-negative")
    timestamp, signatures = _signature_parts(signature_header)
    current_time = int(time.time()) if now is None else now
    if abs(current_time - timestamp) > tolerance_seconds:
        raise SignatureExpired()
    signed = str(timestamp).encode("ascii") + b"." + raw_body
    expected = hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, item) for item in signatures):
        raise InvalidSignature()
    return timestamp


def verify_request(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> ReceiverEvent:
    """Verify headers and raw bytes before parsing the JSON envelope."""
    normalized = {name.lower(): value for name, value in headers.items()}
    signature = normalized.get("webhook-signature")
    event_id = normalized.get("webhook-id")
    event_type = normalized.get("webhook-event")
    if not signature or not event_id or not event_type:
        raise InvalidSignature()
    timestamp = verify_signature(
        raw_body,
        signature,
        secret,
        tolerance_seconds=tolerance_seconds,
        now=now,
    )
    timestamp_header = normalized.get("webhook-timestamp")
    if timestamp_header != str(timestamp):
        raise InvalidSignature()
    try:
        document = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiverVerificationError("Webhook body is not valid JSON") from exc
    if not isinstance(document, dict):
        raise ReceiverVerificationError("Webhook body must be a JSON object")
    if document.get("id") != event_id or document.get("type") != event_type:
        raise ReceiverVerificationError(
            "Webhook headers do not match the signed body"
        )
    return ReceiverEvent(
        event_id=event_id,
        event_type=event_type,
        timestamp=timestamp,
        body=document,
        raw_body=raw_body,
    )


def verify_and_claim(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    deduplicator: DurableDeduplicator,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> ReceiverEvent:
    """Verify then atomically reserve the signed event ID.

    The deduplicator should reserve the ID in the same durable transaction as
    receiver acceptance or application state changes. Return a successful 2xx
    for DuplicateEvent so platform retries stop.
    """
    event = verify_request(
        raw_body,
        headers,
        secret,
        tolerance_seconds=tolerance_seconds,
        now=now,
    )
    if not deduplicator.claim(event.event_id):
        raise DuplicateEvent(event.event_id)
    return event
