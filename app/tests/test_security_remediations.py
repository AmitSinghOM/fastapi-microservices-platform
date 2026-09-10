"""Security-review remediations: throttle purge, body cap, registration."""

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.routers.users as users_router_module
from app.admission import database_now
from app.config import get_settings
from app.lifecycle import LifecycleService
from app.models import LoginThrottle
from app.tests.test_delivery_contracts import worker_settings


@pytest.mark.asyncio
async def test_lifecycle_purges_stale_login_throttles(
    db_session: AsyncSession,
    sqlite_session_factory,
):
    settings = worker_settings()
    now = await database_now(db_session)
    window = timedelta(seconds=settings.login_throttle_window_seconds)

    stale_unlocked = LoginThrottle(
        scope="email:stale@example.com",
        failure_count=3,
        window_started_at=now - window * 3,
        locked_until=None,
        updated_at=now - window * 3,
    )
    stale_expired_lock = LoginThrottle(
        scope="email:expired-lock@example.com",
        failure_count=10,
        window_started_at=now - window * 3,
        locked_until=now - window,
        updated_at=now - window * 2,
    )
    fresh = LoginThrottle(
        scope="email:fresh@example.com",
        failure_count=2,
        window_started_at=now,
        locked_until=None,
        updated_at=now,
    )
    actively_locked_but_old = LoginThrottle(
        scope="email:locked@example.com",
        failure_count=10,
        window_started_at=now - window * 3,
        locked_until=now + window,
        updated_at=now - window * 2,
    )
    db_session.add_all(
        (stale_unlocked, stale_expired_lock, fresh, actively_locked_but_old)
    )
    await db_session.commit()

    result = await LifecycleService(
        sqlite_session_factory, settings
    ).run_once()

    assert result.login_throttles_purged == 2
    remaining = set(
        await db_session.scalars(select(LoginThrottle.scope))
    )
    assert remaining == {
        "email:fresh@example.com",
        "email:locked@example.com",
    }


@pytest.mark.asyncio
async def test_oversized_declared_body_is_refused_before_parsing(
    client: AsyncClient,
):
    limit = get_settings().webhook_payload_max_bytes + 65_536
    response = await client.post(
        "/v1/events",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(limit + 1),
        },
        content=b"",
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_registration_can_be_disabled(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = worker_settings().model_copy(
        update={"registration_enabled": False}
    )
    monkeypatch.setattr(
        users_router_module, "get_settings", lambda: settings
    )
    refused = await client.post(
        "/users/",
        json={
            "email": "closed@example.com",
            "name": "Closed",
            "password": "correct horse battery staple",
        },
    )
    assert refused.status_code == 403, refused.text

    from app.models import User

    count = await db_session.scalar(select(func.count(User.id)))
    assert count == 0


@pytest.mark.asyncio
async def test_email_case_is_normalized_for_identity(client: AsyncClient):
    """One mailbox, one account: case variants must not fork identities."""
    password = "correct horse battery staple"
    created = await client.post(
        "/users/",
        json={"email": "Amit@Example.com", "name": "A", "password": password},
    )
    assert created.status_code == 201, created.text
    assert created.json()["email"] == "amit@example.com"

    duplicate = await client.post(
        "/users/",
        json={"email": "AMIT@example.com", "name": "B", "password": password},
    )
    assert duplicate.status_code == 409, duplicate.text

    logged_in = await client.post(
        "/auth/login",
        data={"username": "aMiT@eXaMpLe.CoM", "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
