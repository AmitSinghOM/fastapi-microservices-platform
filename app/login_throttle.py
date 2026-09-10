"""Database-backed per-account login throttling.

The in-process ``RateLimitMiddleware`` is defense in depth only: its window
multiplies by API replica count and resets on restart. This module keeps the
authoritative brute-force budget in the shared database, keyed by the
lowercased target account, so the limit holds across a horizontally scaled
control plane.

Semantics: failures inside a rolling window accumulate per account. Reaching
the configured maximum locks the account scope for a fixed period and every
further attempt while locked returns ``429`` with ``Retry-After``. Any
successful login clears the account's state. The throttle is keyed by the
attacked account, not the caller address, so a distributed attack against
one account is still bounded while other accounts stay unaffected.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admission import _insert_do_nothing, database_now
from app.config import get_settings
from app.exceptions import QuotaExceededError
from app.models import LoginThrottle

_SCOPE_MAX = 340


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def login_scope(email: str) -> str:
    return f"email:{email.strip().lower()}"[:_SCOPE_MAX]


async def _locked_row(
    db: AsyncSession, scope: str, for_update: bool
) -> LoginThrottle | None:
    statement = select(LoginThrottle).where(LoginThrottle.scope == scope)
    if for_update and db.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update()
    return await db.scalar(statement)


def _retry_after(now: datetime, locked_until: datetime) -> int:
    return max(1, math.ceil((_aware(locked_until) - now).total_seconds()))


async def check_login_allowed(db: AsyncSession, email: str) -> None:
    """Refuse the attempt while the account scope is locked."""
    row = await _locked_row(db, login_scope(email), for_update=False)
    if row is None or row.locked_until is None:
        return
    now = await database_now(db)
    if _aware(row.locked_until) > now:
        raise QuotaExceededError(
            "login_attempts", _retry_after(now, row.locked_until)
        )


async def record_login_failure(db: AsyncSession, email: str) -> None:
    """Count a failed attempt; lock the scope when the budget is spent.

    Commits its own transaction so the throttle state survives even though
    the surrounding request fails. Row creation uses the same dialect-aware
    insert-do-nothing idiom as admission state: two concurrent first
    failures must both count, never surface a primary-key IntegrityError
    on the login path.
    """
    settings = get_settings()
    scope = login_scope(email)
    now = await database_now(db)
    window = timedelta(seconds=settings.login_throttle_window_seconds)

    await _insert_do_nothing(
        db,
        LoginThrottle,
        {
            "scope": scope,
            "failure_count": 0,
            "window_started_at": now,
            "locked_until": None,
            "updated_at": now,
        },
        "scope",
    )
    row = await _locked_row(db, scope, for_update=True)
    if row is None:  # pragma: no cover - row was just ensured
        await db.rollback()
        return
    expired_lock = (
        row.locked_until is not None
        and _aware(row.locked_until) <= now
    )
    if _aware(row.window_started_at) + window <= now or expired_lock:
        row.failure_count = 1
        row.window_started_at = now
        row.locked_until = None
    else:
        row.failure_count += 1
    if row.failure_count >= settings.login_throttle_max_failures:
        row.locked_until = now + timedelta(
            seconds=settings.login_throttle_lockout_seconds
        )
    row.updated_at = now
    await db.commit()


async def record_login_success(db: AsyncSession, email: str) -> None:
    """A successful login clears the account's throttle state."""
    await db.execute(
        delete(LoginThrottle).where(LoginThrottle.scope == login_scope(email))
    )
    await db.commit()
