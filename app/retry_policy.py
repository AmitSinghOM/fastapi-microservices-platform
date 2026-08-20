"""Pure bounded retry policy helpers for outbound delivery attempts."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_retryable_status(status_code: int | None) -> bool:
    return status_code in RETRYABLE_HTTP_STATUSES or bool(
        status_code is not None and 500 <= status_code <= 599
    )


def parse_retry_after(
    value: str | None,
    now: datetime,
    maximum_seconds: int,
) -> float | None:
    """Parse delta-seconds or an HTTP date and clamp it to a safe maximum."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        if candidate.isascii() and candidate.isdigit():
            delay = float(int(candidate))
        else:
            target = parsedate_to_datetime(candidate)
            delay = max(0.0, (aware(target) - aware(now)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
    return min(float(maximum_seconds), delay)


def jittered_backoff(
    attempt_number: int,
    base_seconds: float,
    cap_seconds: float,
) -> float:
    ceiling = min(
        cap_seconds,
        base_seconds * (2 ** max(0, attempt_number - 1)),
    )
    return random.uniform(0.0, ceiling)
