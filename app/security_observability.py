"""Low-cardinality, destination-free egress security observability."""

from __future__ import annotations

import logging
from collections import Counter
from enum import StrEnum
from threading import Lock

logger = logging.getLogger("app.security.egress")


class SecurityLayer(StrEnum):
    ADMISSION = "admission"
    ATTEMPT = "attempt"
    PROXY = "proxy"


class SecurityDenyReason(StrEnum):
    URL_TOO_LONG = "url_too_long"
    INVALID_URL = "invalid_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    CREDENTIALS_FORBIDDEN = "credentials_forbidden"
    FRAGMENT_FORBIDDEN = "fragment_forbidden"
    PORT_NOT_ALLOWED = "port_not_allowed"
    LOCALHOST_FORBIDDEN = "localhost_forbidden"
    DNS_UNRESOLVED = "dns_unresolved"
    NON_GLOBAL_ADDRESS = "non_global_address"
    PROXY_CONNECT_DENIED = "proxy_connect_denied"


_counts: Counter[tuple[SecurityLayer, SecurityDenyReason]] = Counter()
_lock = Lock()


def record_security_deny(
    layer: SecurityLayer, reason: SecurityDenyReason
) -> None:
    """Count and audit a deny without accepting destination context."""
    with _lock:
        _counts[(layer, reason)] += 1
    logger.warning(
        "webhook_egress_denied layer=%s reason=%s", layer.value, reason.value
    )


def security_deny_counts() -> dict[tuple[str, str], int]:
    """Return a low-cardinality snapshot suitable for metric export."""
    with _lock:
        return {
            (layer.value, reason.value): count
            for (layer, reason), count in _counts.items()
        }


def reset_security_deny_counts() -> None:
    """Reset process-local counters for isolated tests."""
    with _lock:
        _counts.clear()
