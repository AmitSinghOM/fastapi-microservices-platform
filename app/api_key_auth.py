"""Authentication for event ingestion, separate from JWT management auth."""

import re
from datetime import datetime, timezone

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admission import database_now
from app.api_key_usage import api_key_usage_tracker
from app.config import get_settings
from app.db import get_db
from app.exceptions import UnauthorizedError
from app.models import ApiKey, Organization, Project
from app.webhook_security import verify_api_key

_API_KEY_PATTERN = re.compile(
    r"\Awhk_(?P<prefix>[0-9a-f]{12})_[A-Za-z0-9_-]{43}\Z"
)
_DIGEST_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_REQUIRED_SCOPE = "events:write"


def invalid_api_key() -> UnauthorizedError:
    return UnauthorizedError("Invalid API key", auth_scheme="X-API-Key")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def get_api_key_project(
    plaintext: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Project:
    match = _API_KEY_PATTERN.fullmatch(plaintext or "")
    if match is None:
        raise invalid_api_key()

    rows = (
        await db.execute(
            select(ApiKey, Project, Organization)
            .join(Project, Project.id == ApiKey.project_id)
            .join(Organization, Organization.id == Project.organization_id)
            .where(ApiKey.key_prefix == match.group("prefix"))
        )
    ).all()

    # A unique index enforces one row per prefix today, but authentication
    # must not depend on that schema detail: verify the peppered digest
    # against every returned candidate and select the verifying key.
    settings = get_settings()
    row = next(
        (
            candidate
            for candidate in rows
            if _DIGEST_PATTERN.fullmatch(candidate[0].key_digest)
            and verify_api_key(
                plaintext or "",
                candidate[0].key_digest,
                settings.api_key_pepper,
            )
        ),
        None,
    )
    if row is None:
        raise invalid_api_key()

    api_key, project, organization = row
    scopes = api_key.scopes
    now = await database_now(db)
    if (
        not api_key.is_active
        or api_key.revoked_at is not None
        or not isinstance(scopes, list)
        or _REQUIRED_SCOPE not in scopes
        or (
            api_key.expires_at is not None
            and _aware(api_key.expires_at) <= now
        )
        or not project.is_active
        or organization.lifecycle_state != "active"
    ):
        raise invalid_api_key()

    await api_key_usage_tracker.record(api_key.id, now)
    return project
