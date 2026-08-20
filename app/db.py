import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings, get_settings
from app.observability import (
    instrument_engine,
    observe_pool_acquisition,
    set_pool_capacity,
)


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True)
class DatabaseResources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def close(self) -> None:
        await self.engine.dispose()


def create_database_resources(
    settings: Settings,
    role: Literal["api", "worker"],
) -> DatabaseResources:
    """Create a role-bounded engine; SQLite keeps its compatible defaults."""
    options: dict[str, object] = {
        "echo": settings.debug,
        "pool_pre_ping": True,
    }
    if not settings.database_url.startswith("sqlite"):
        prefix = "api_db" if role == "api" else "worker_db"
        options.update(
            pool_size=getattr(settings, f"{prefix}_pool_size"),
            max_overflow=getattr(settings, f"{prefix}_max_overflow"),
            pool_timeout=getattr(settings, f"{prefix}_pool_timeout_seconds"),
        )
    database_engine = create_async_engine(settings.database_url, **options)
    instrument_engine(database_engine, role)
    if settings.database_url.startswith("sqlite"):
        set_pool_capacity(role, 1)
    else:
        prefix = "api_db" if role == "api" else "worker_db"
        set_pool_capacity(
            role,
            getattr(settings, f"{prefix}_pool_size")
            + getattr(settings, f"{prefix}_max_overflow"),
        )
    session_factory = async_sessionmaker(
        database_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    if settings.database_url.startswith("sqlite"):

        @event.listens_for(database_engine.sync_engine, "connect")
        def enable_sqlite_foreign_keys(
            dbapi_connection,
            connection_record,
        ) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return DatabaseResources(database_engine, session_factory)


settings = get_settings()
api_database = create_database_resources(settings, "api")
engine = api_database.engine
async_session = api_database.session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    started = asyncio.get_running_loop().time()
    acquired = False
    try:
        async with engine.connect() as connection:
            acquired = True
            observe_pool_acquisition(
                asyncio.get_running_loop().time() - started
            )
            async with AsyncSession(
                bind=connection, expire_on_commit=False
            ) as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise
    except Exception:
        if not acquired:
            observe_pool_acquisition(
                asyncio.get_running_loop().time() - started, "error"
            )
        raise


async def init_db() -> None:
    """Create tables for local development and tests only."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def database_is_ready() -> bool:
    """Run a bounded connectivity probe for readiness checks."""
    try:
        async with asyncio.timeout(settings.database_health_timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, TimeoutError, OSError):
        return False


async def close_database() -> None:
    """Release API-process pooled connections during graceful shutdown."""
    await api_database.close()
