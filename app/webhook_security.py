"""Webhook credentials, canonical signing, and SSRF-resistant URL checks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from base64 import b64encode, urlsafe_b64encode
from typing import Any
from urllib.parse import urlsplit

from app.security_observability import SecurityDenyReason


class UnsafeWebhookUrl(ValueError):
    """Raised with a bounded reason when a target violates egress policy."""

    def __init__(self, message: str, reason: SecurityDenyReason):
        super().__init__(message)
        self.reason = reason


def generate_api_key() -> tuple[str, str]:
    """Return a plaintext API key and its non-secret lookup prefix."""
    prefix = secrets.token_hex(6)
    return f"whk_{prefix}_{secrets.token_urlsafe(32)}", prefix


def digest_api_key(plaintext: str, pepper: str) -> str:
    return hmac.new(
        pepper.encode("utf-8"), plaintext.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_api_key(plaintext: str, expected_digest: str, pepper: str) -> bool:
    return hmac.compare_digest(
        digest_api_key(plaintext, pepper), expected_digest
    )


def endpoint_secret(
    signing_key: str, endpoint_public_id: str, secret_version: int
) -> str:
    material = f"endpoint:{endpoint_public_id}:v{secret_version}".encode()
    digest = hmac.new(signing_key.encode(), material, hashlib.sha256).digest()
    return "whsec_" + urlsafe_b64encode(digest).decode().rstrip("=")


def endpoint_secret_digest(
    signing_key: str, endpoint_public_id: str, secret_version: int
) -> bytes:
    """The raw derived key bytes shared by both secret serializations."""
    material = f"endpoint:{endpoint_public_id}:v{secret_version}".encode()
    return hmac.new(signing_key.encode(), material, hashlib.sha256).digest()


def endpoint_secret_standard(
    signing_key: str, endpoint_public_id: str, secret_version: int
) -> str:
    """Standard Webhooks serialization: ``whsec_`` + padded base64.

    Any Standard Webhooks library decodes the base64 after the prefix and
    uses the resulting bytes as the HMAC key, so this string is directly
    usable with the spec's ecosystem. The ``legacy`` serialization encodes
    the same digest but is itself the key material as an ASCII string.
    """
    digest = endpoint_secret_digest(
        signing_key, endpoint_public_id, secret_version
    )
    return "whsec_" + b64encode(digest).decode()


def canonical_json(value: Any) -> bytes:
    """Encode stable, compact UTF-8 JSON and reject NaN/infinity."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def event_type_matches(event_type: str, patterns: object) -> bool:
    """Decide whether an endpoint subscription list accepts an event type.

    ``None`` (no filter) accepts everything. A list accepts exact matches
    and trailing ``prefix.*`` wildcards, where ``order.*`` matches
    ``order.created`` but neither ``order`` nor ``orders.created``.
    Malformed stored state fails closed for that entry.
    """
    if patterns is None:
        return True
    if not isinstance(patterns, list):
        return False
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        if pattern.endswith(".*"):
            if event_type.startswith(pattern[:-1]):
                return True
        elif event_type == pattern:
            return True
    return False


def sign_payload(
    payload_bytes: bytes, secret: str, timestamp: int | None = None
) -> tuple[int, str]:
    timestamp = timestamp if timestamp is not None else int(time.time())
    signed = str(timestamp).encode("ascii") + b"." + payload_bytes
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return timestamp, f"t={timestamp},v1={digest}"


def sign_payload_standard(
    event_id: str,
    payload_bytes: bytes,
    key: bytes,
    timestamp: int | None = None,
) -> tuple[int, str]:
    """Standard Webhooks signature: ``v1,<base64>``.

    Signed content is ``event_id.timestamp.body`` and the HMAC key is the
    raw derived digest, matching the specification exactly so any Standard
    Webhooks library verifies the result. Event IDs are UUIDs and
    timestamps are integers, so neither can contain the ``.`` delimiter.
    """
    timestamp = timestamp if timestamp is not None else int(time.time())
    signed = (
        f"{event_id}.{timestamp}.".encode("ascii") + payload_bytes
    )
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return timestamp, "v1," + b64encode(digest).decode()


def _require_global(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        raise
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise UnsafeWebhookUrl(
            "Webhook host resolves to a non-global address",
            SecurityDenyReason.NON_GLOBAL_ADDRESS,
        )


async def validate_webhook_url(
    url: str, allow_http: bool = False, allow_private: bool = False
) -> str:
    """Resolve every answer and validate a target immediately before send.

    The dedicated egress proxy repeats destination resolution and network
    policy enforcement after this defense-in-depth application check.

    ``allow_private`` is a development-only escape hatch (enforced at
    configuration load): it permits localhost and non-global targets so the
    local quick-start receiver can complete a signed delivery. Scheme,
    credential, fragment, length, and DNS-resolvability checks still apply.
    """
    if len(url) > 2_048:
        raise UnsafeWebhookUrl(
            "Webhook URL is too long", SecurityDenyReason.URL_TOO_LONG
        )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeWebhookUrl(
            "Webhook URL is invalid", SecurityDenyReason.INVALID_URL
        ) from exc
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme.lower() not in allowed_schemes:
        raise UnsafeWebhookUrl(
            "Webhook URL scheme is not allowed",
            SecurityDenyReason.SCHEME_NOT_ALLOWED,
        )
    if not parsed.hostname:
        raise UnsafeWebhookUrl(
            "Webhook URL must have a host",
            SecurityDenyReason.INVALID_URL,
        )
    if parsed.username or parsed.password:
        raise UnsafeWebhookUrl(
            "Webhook URL credentials are not allowed",
            SecurityDenyReason.CREDENTIALS_FORBIDDEN,
        )
    if parsed.fragment:
        raise UnsafeWebhookUrl(
            "Webhook URL fragments are not allowed",
            SecurityDenyReason.FRAGMENT_FORBIDDEN,
        )
    if not allow_http and (port or 443) != 443:
        raise UnsafeWebhookUrl(
            "Webhook URL port must be 443",
            SecurityDenyReason.PORT_NOT_ALLOWED,
        )
    hostname = parsed.hostname.rstrip(".").lower()
    if not allow_private and (
        hostname == "localhost" or hostname.endswith(".localhost")
    ):
        raise UnsafeWebhookUrl(
            "Localhost webhook targets are not allowed",
            SecurityDenyReason.LOCALHOST_FORBIDDEN,
        )
    try:
        _require_global(hostname)
        return url
    except UnsafeWebhookUrl:
        # The hostname is a literal non-global IP address.
        if allow_private:
            return url
        raise
    except ValueError:
        pass
    try:
        results = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeWebhookUrl(
            "Webhook host could not be resolved",
            SecurityDenyReason.DNS_UNRESOLVED,
        ) from exc
    addresses = {result[4][0] for result in results}
    if not addresses:
        raise UnsafeWebhookUrl(
            "Webhook host did not resolve",
            SecurityDenyReason.DNS_UNRESOLVED,
        )
    if not allow_private:
        for address in addresses:
            _require_global(str(address))
    return url
