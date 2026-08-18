"""Authentication for event ingestion, separate from JWT management auth."""

from datetime import datetime, timezone

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.exceptions import UnauthorizedError
from app.models import ApiKey, Project
from app.webhook_security import verify_api_key


def invalid_api_key() -> UnauthorizedError:
    return UnauthorizedError("Invalid API key", auth_scheme="X-API-Key")


async def get_api_key_project(
    plaintext: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Project:
    if not plaintext:
        raise invalid_api_key()
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != "whk":
        raise invalid_api_key()
    api_key = await db.scalar(
        select(ApiKey).where(
            ApiKey.key_prefix == parts[1], ApiKey.is_active.is_(True)
        )
    )
    settings = get_settings()
    if api_key is None or not verify_api_key(
        plaintext, api_key.key_digest, settings.api_key_pepper
    ):
        raise invalid_api_key()
    project = await db.get(Project, api_key.project_id)
    if project is None or not project.is_active:
        raise invalid_api_key()
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    return project
