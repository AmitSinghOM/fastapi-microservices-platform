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
from base64 import urlsafe_b64encode
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


def canonical_json(value: Any) -> bytes:
    """Encode stable, compact UTF-8 JSON and reject NaN/infinity."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sign_payload(
    payload_bytes: bytes, secret: str, timestamp: int | None = None
) -> tuple[int, str]:
    timestamp = timestamp if timestamp is not None else int(time.time())
    signed = str(timestamp).encode("ascii") + b"." + payload_bytes
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return timestamp, f"t={timestamp},v1={digest}"


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


async def validate_webhook_url(url: str, allow_http: bool = False) -> str:
    """Resolve every answer and validate a target immediately before send.

    The dedicated egress proxy repeats destination resolution and network
    policy enforcement after this defense-in-depth application check.
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
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeWebhookUrl(
            "Localhost webhook targets are not allowed",
            SecurityDenyReason.LOCALHOST_FORBIDDEN,
        )
    try:
        _require_global(hostname)
        return url
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
    for address in addresses:
        _require_global(str(address))
    return url
