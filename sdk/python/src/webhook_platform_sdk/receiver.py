"""Raw-byte receiver verification and durable deduplication helpers."""

from __future__ import annotations

import base64
import binascii
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
    """Parse the legacy ``t=<seconds>,v1=<hex>`` header format.

    Only tokens in the legacy shape participate; unrecognized
    space-delimited tokens (for example Standard Webhooks ``v1,<base64>``
    entries) are ignored rather than treated as malformed, so a receiver on
    this SDK survives header formats it does not use.
    """
    timestamp: int | None = None
    signatures: list[str] = []
    for token in header.split():
        if "=" not in token.partition(",")[0]:
            continue
        for item in token.split(","):
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


def _standard_signatures(header: str) -> list[str]:
    """Collect Standard Webhooks ``v1,<base64>`` tokens, ignoring others.

    Non-ASCII values are dropped here so a malformed or malicious header
    fails verification (``InvalidSignature``) instead of raising
    ``TypeError`` from the constant-time comparison.
    """
    signatures: list[str] = []
    for token in header.split():
        version, separator, value = token.partition(",")
        if separator and version == "v1" and value and value.isascii():
            signatures.append(value)
    return signatures


def _secret_key_bytes(secret: str) -> bytes | None:
    """Decode either secret serialization to the raw digest bytes.

    Accepts ``whsec_`` + padded standard base64 (the standard form) and
    ``whsec_`` + unpadded base64url (the legacy form). Returns ``None``
    when the secret does not decode, in which case only legacy
    verification (which keys on the ASCII string itself) is possible.

    ``validate=True`` is load-bearing: without it ``b64decode`` silently
    drops characters outside its alphabet, so a legacy-form (base64url)
    secret containing ``-``/``_`` would "decode" through the standard
    decoder into wrong key bytes instead of falling through to the
    urlsafe decoder.
    """
    if not secret.startswith("whsec_"):
        return None
    encoded = secret.removeprefix("whsec_")
    padded = encoded + "=" * (-len(encoded) % 4)
    # urlsafe_b64decode has no validate parameter and would also silently
    # accept the wrong alphabet, so translate explicitly and validate both.
    candidates = (padded, padded.replace("-", "+").replace("_", "/"))
    for candidate in candidates:
        try:
            return base64.b64decode(candidate, validate=True)
        except (ValueError, binascii.Error):
            continue
    return None


def _header_has_legacy_token(header: str) -> bool:
    return any("=" in token.partition(",")[0] for token in header.split())


def verify_signature(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> int:
    """Verify a legacy HMAC over timestamp + period + exact body bytes."""
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


def verify_signature_standard(
    raw_body: bytes,
    event_id: str,
    timestamp_header: str | None,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> int:
    """Verify a Standard Webhooks signature (``v1,<base64>``).

    Signed content is ``event_id.timestamp.body``; the key is the decoded
    secret digest. Either secret serialization is accepted.
    """
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be non-negative")
    if not timestamp_header or not timestamp_header.isdigit():
        raise InvalidSignature()
    timestamp = int(timestamp_header)
    current_time = int(time.time()) if now is None else now
    if abs(current_time - timestamp) > tolerance_seconds:
        raise SignatureExpired()
    signatures = _standard_signatures(signature_header)
    key = _secret_key_bytes(secret)
    if not signatures or key is None:
        raise InvalidSignature()
    signed = f"{event_id}.{timestamp}.".encode("ascii") + raw_body
    expected = base64.b64encode(
        hmac.new(key, signed, hashlib.sha256).digest()
    ).decode()
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
    """Verify headers and raw bytes before parsing the JSON envelope.

    Scheme auto-detection (ADR 0002): a header containing a legacy
    ``t=…,v1=…`` token verifies as legacy; otherwise ``v1,<base64>``
    tokens verify as Standard Webhooks. Either secret serialization is
    accepted, so a receiver can upgrade this SDK before its endpoint
    switches schemes.
    """
    normalized = {name.lower(): value for name, value in headers.items()}
    signature = normalized.get("webhook-signature")
    event_id = normalized.get("webhook-id")
    event_type = normalized.get("webhook-event")
    if not signature or not event_id or not event_type:
        raise InvalidSignature()
    timestamp_header = normalized.get("webhook-timestamp")
    if _header_has_legacy_token(signature):
        timestamp = verify_signature(
            raw_body,
            signature,
            secret,
            tolerance_seconds=tolerance_seconds,
            now=now,
        )
        if timestamp_header != str(timestamp):
            raise InvalidSignature()
    else:
        timestamp = verify_signature_standard(
            raw_body,
            event_id,
            timestamp_header,
            signature,
            secret,
            tolerance_seconds=tolerance_seconds,
            now=now,
        )
    try:
        document = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiverVerificationError(
            "Webhook body is not valid JSON"
        ) from exc
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
