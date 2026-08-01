import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.db import Base, get_db
from app.middleware import reset_rate_limits

# Configure pytest-asyncio mode
pytest_plugins = ('pytest_asyncio',)

# Test database
TEST_DATABASE_URL = "sqlite+aiosqlite:///./test.db"
test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_async_session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def clear_rate_limits():
    """Rate-limit counters are process-wide, so reset them between tests."""
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest_asyncio.fixture
async def db_session():
    """Create test database session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with test_async_session() as session:
        yield session
    
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(db_session):
    """Create test client with overridden dependencies."""
    
    async def override_get_db():
        yield db_session
    
    app.dependency_overrides[get_db] = override_get_db
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac
    
    app.dependency_overrides.clear()


async def register_and_login(
    client: AsyncClient,
    email: str,
    password: str = "correct horse battery staple",
    name: str = "Test User",
) -> tuple[dict, str]:
    """Register a user and return ``(user, access_token)``.

    Goes through the real login endpoint rather than minting a token directly,
    so the tests exercise the actual authentication path.
    """
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
    """An authenticated user: ``(user, headers)``."""
    user, token = await register_and_login(client, "owner@example.com")
    return user, {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def other_auth(client: AsyncClient):
    """A second, unrelated authenticated user, for isolation tests."""
    user, token = await register_and_login(
        client, "intruder@example.com", name="Someone Else")
    return user, {"Authorization": f"Bearer {token}"}
