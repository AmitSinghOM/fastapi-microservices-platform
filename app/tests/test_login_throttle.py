"""Shared per-account login brute-force throttling."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.login_throttle import login_scope
from app.models import LoginThrottle
from app.tests.conftest import register_and_login

PASSWORD = "correct horse battery staple"


async def _register(client: AsyncClient, email: str) -> None:
    created = await client.post(
        "/users/",
        json={"email": email, "name": "Throttle", "password": PASSWORD},
    )
    assert created.status_code == 201, created.text


async def _fail_login(client: AsyncClient, email: str) -> int:
    response = await client.post(
        "/auth/login",
        data={"username": email, "password": "wrong-password"},
    )
    return response.status_code


@pytest.mark.asyncio
async def test_account_locks_after_failure_budget(
    client: AsyncClient,
    db_session: AsyncSession,
):
    email = "locked@example.com"
    await _register(client, email)

    for _ in range(10):
        assert await _fail_login(client, email) == 401

    locked = await client.post(
        "/auth/login",
        data={"username": email, "password": PASSWORD},
    )
    assert locked.status_code == 429, locked.text
    assert int(locked.headers["Retry-After"]) >= 1
    assert locked.json()["error"]["code"] == "QUOTA_EXCEEDED"

    row = await db_session.scalar(
        select(LoginThrottle).where(LoginThrottle.scope == login_scope(email))
    )
    assert row is not None and row.locked_until is not None


@pytest.mark.asyncio
async def test_success_clears_throttle_state(
    client: AsyncClient,
    db_session: AsyncSession,
):
    email = "recovers@example.com"
    await _register(client, email)

    for _ in range(3):
        assert await _fail_login(client, email) == 401

    ok = await client.post(
        "/auth/login",
        data={"username": email, "password": PASSWORD},
    )
    assert ok.status_code == 200, ok.text

    row = await db_session.scalar(
        select(LoginThrottle).where(LoginThrottle.scope == login_scope(email))
    )
    assert row is None


@pytest.mark.asyncio
async def test_scope_is_case_insensitive_and_lock_hides_account_state(
    client: AsyncClient,
    db_session: AsyncSession,
):
    # Failures against different casings of one address share a budget.
    email = "case@example.com"
    await _register(client, email)
    for _ in range(5):
        assert await _fail_login(client, email) == 401
    for _ in range(5):
        assert await _fail_login(client, "CASE@example.com") == 401
    locked = await client.post(
        "/auth/login",
        data={"username": email, "password": PASSWORD},
    )
    assert locked.status_code == 429


@pytest.mark.asyncio
async def test_unknown_account_locks_identically(
    client: AsyncClient,
    db_session: AsyncSession,
):
    # An unregistered address locks exactly like a real one, so the
    # throttle does not become an account-enumeration oracle.
    ghost = "ghost@example.com"
    for _ in range(10):
        assert await _fail_login(client, ghost) == 401
    assert await _fail_login(client, ghost) == 429


@pytest.mark.asyncio
async def test_existing_login_flow_unaffected(client: AsyncClient):
    user, token = await register_and_login(client, "normal@example.com")
    assert user["email"] == "normal@example.com"
    assert token


@pytest.mark.asyncio
async def test_failure_recording_survives_concurrent_row_creation(
    client: AsyncClient,
    db_session: AsyncSession,
    sqlite_session_factory,
):
    """Regression: a bare INSERT raced a concurrent first failure into a
    primary-key IntegrityError (a 500 on the login path). The ensure-row
    upsert must absorb a row that appeared between check and write."""
    from app.admission import database_now
    from app.login_throttle import record_login_failure
    from app.models import LoginThrottle as Throttle

    email = "raced@example.com"
    scope = login_scope(email)

    # Simulate the concurrent winner: another session creates the row first.
    async with sqlite_session_factory() as other:
        now = await database_now(other)
        other.add(
            Throttle(
                scope=scope,
                failure_count=1,
                window_started_at=now,
                locked_until=None,
                updated_at=now,
            )
        )
        await other.commit()

    # The "loser" records its failure against the pre-existing row:
    # no IntegrityError, and both failures count.
    await record_login_failure(db_session, email)
    row = await db_session.scalar(
        select(LoginThrottle).where(LoginThrottle.scope == scope)
    )
    assert row is not None and row.failure_count == 2


@pytest.mark.asyncio
async def test_doubleinsert_do_nothing_is_safe(
    db_session: AsyncSession, sqlite_session_factory
):
    """The ensure-row idiom itself must tolerate both racers inserting."""
    del db_session  # fixture creates the schema
    from app.admission import insert_do_nothing, database_now
    from app.models import LoginThrottle as Throttle

    scope = login_scope("双insert@example.com")
    async with sqlite_session_factory() as first:
        async with sqlite_session_factory() as second:
            now = await database_now(first)
            values = {
                "scope": scope,
                "failure_count": 0,
                "window_started_at": now,
                "locked_until": None,
                "updated_at": now,
            }
            await insert_do_nothing(first, Throttle, values, "scope")
            await first.commit()
            await insert_do_nothing(second, Throttle, dict(values), "scope")
            await second.commit()  # must not raise
