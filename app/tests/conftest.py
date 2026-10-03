import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import Base, get_db
from app.main import app
from app.middleware import reset_rate_limits
from app.security_observability import reset_security_deny_counts

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(autouse=True)
def clear_rate_limits():
    """Reset process-local limits and security counters between tests."""
    reset_rate_limits()
    reset_security_deny_counts()
    yield
    reset_rate_limits()
    reset_security_deny_counts()


@pytest_asyncio.fixture
async def sqlite_session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", echo=False
    )
    try:
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(sqlite_session_factory) -> AsyncIterator[AsyncSession]:
    """Provide an isolated SQLite session for fast API tests."""
    async with sqlite_session_factory() as session:
        connection = await session.connection()
        await connection.run_sync(Base.metadata.create_all)
        await session.commit()
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """Create a client whose request sessions use the isolated database."""

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as api_client:
        yield api_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def postgres_session_factory():
    """Create an isolated PostgreSQL schema when TEST_POSTGRES_URL is set.

    The fixture never drops the configured database or its public schema.
    Each test session receives a random schema that is removed on teardown.
    """
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    if not database_url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use postgresql+asyncpg")

    schema = f"webhook_test_{uuid4().hex}"
    admin_engine = create_async_engine(
        database_url, isolation_level="AUTOCOMMIT"
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine: AsyncEngine = create_async_engine(
        database_url,
        connect_args={"server_settings": {"search_path": schema}},
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield session_factory
    finally:
        await engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            )
        await admin_engine.dispose()


async def register_and_login(
    client: AsyncClient,
    email: str,
    password: str = "correct horse battery staple",
    name: str = "Test User",
) -> tuple[dict, str]:
    """Register a user and return the user and a real access token."""
    created = await client.post(
        "/users/",
        json={"email": email, "name": name, "password": password},
    )
    assert created.status_code == 201, created.text
    user = created.json()

    logged_in = await client.post(
        "/auth/login",
        data={"username": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    return user, logged_in.json()["access_token"]


@pytest_asyncio.fixture
async def auth(client: AsyncClient):
    """Return an authenticated user and bearer headers."""
    user, token = await register_and_login(client, "owner@example.com")
    return user, {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def other_auth(client: AsyncClient):
    """Return an unrelated authenticated user for isolation tests."""
    user, token = await register_and_login(
        client,
        "intruder@example.com",
        name="Someone Else",
    )
    return user, {"Authorization": f"Bearer {token}"}
