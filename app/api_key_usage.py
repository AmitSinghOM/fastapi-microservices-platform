"""Eventually consistent, coalesced API-key usage timestamps."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import async_session
from app.models import ApiKey

logger = logging.getLogger(__name__)


class ApiKeyUsageTracker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.interval = settings.api_key_usage_flush_seconds
        self.max_entries = settings.api_key_usage_max_entries
        self._pending: dict[int, datetime] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def record(self, api_key_id: int, used_at: datetime) -> None:
        if self._task is None:
            return
        async with self._lock:
            existing = self._pending.get(api_key_id)
            if existing is not None or len(self._pending) < self.max_entries:
                self._pending[api_key_id] = max(existing or used_at, used_at)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        await self._task
        self._task = None
        await self.flush()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval
                )
            except TimeoutError:
                await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    keys = await session.scalars(
                        select(ApiKey).where(ApiKey.id.in_(pending))
                    )
                    for api_key in keys:
                        used_at = pending[api_key.id]
                        last_used_at = api_key.last_used_at
                        if (
                            last_used_at is not None
                            and last_used_at.tzinfo is None
                        ):
                            last_used_at = last_used_at.replace(
                                tzinfo=timezone.utc
                            )
                        if last_used_at is None or last_used_at < used_at:
                            api_key.last_used_at = used_at
        except Exception as exc:
            logger.warning(
                "Could not flush API-key usage timestamps (%s)",
                type(exc).__name__,
            )
            async with self._lock:
                for api_key_id, used_at in pending.items():
                    existing = self._pending.get(api_key_id)
                    self._pending[api_key_id] = max(
                        existing or used_at, used_at
                    )


settings = get_settings()
api_key_usage_tracker = ApiKeyUsageTracker(async_session, settings)
