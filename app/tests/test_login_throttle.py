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
        select(LoginThrottle).where(
            LoginThrottle.scope == login_scope(email)
        )
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
        select(LoginThrottle).where(
            LoginThrottle.scope == login_scope(email)
        )
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
