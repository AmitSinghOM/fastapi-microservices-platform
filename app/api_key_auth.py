"""Project-bound API credentials and opt-in machine management auth."""

import re
from datetime import datetime, timezone
from typing import cast

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_active_user, get_current_user, oauth2_scheme
from app.admission import database_now
from app.api_key_usage import api_key_usage_tracker
from app.config import get_settings
from app.db import get_db
from app.exceptions import UnauthorizedError
from app.models import ApiKey, Organization, Project, User
from app.principals import API_KEY_SCOPES, MachinePrincipal
from app.webhook_security import verify_api_key

_API_KEY_PATTERN = re.compile(
    r"\Awhk_(?P<prefix>[0-9a-f]{12})_[A-Za-z0-9_-]{43}\Z"
)
_DIGEST_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


def invalid_api_key() -> UnauthorizedError:
    return UnauthorizedError("Invalid API key", auth_scheme="X-API-Key")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def authenticate_api_key(
    plaintext: str | None, db: AsyncSession
) -> tuple[ApiKey, Project]:
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
        or not scopes
        or any(
            not isinstance(scope, str) or scope not in API_KEY_SCOPES
            for scope in scopes
        )
        or (
            api_key.expires_at is not None
            and _aware(api_key.expires_at) <= now
        )
        or not project.is_active
        or organization.lifecycle_state != "active"
    ):
        raise invalid_api_key()

    await api_key_usage_tracker.record(api_key.id, now)
    return api_key, project


async def get_api_key_project(
    plaintext: str | None = Header(default=None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> Project:
    api_key, project = await authenticate_api_key(plaintext, db)
    if "events:write" not in api_key.scopes:
        raise invalid_api_key()
    return project


async def get_management_principal(
    plaintext: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | MachinePrincipal:
    if plaintext is not None:
        if authorization is not None:
            raise invalid_api_key()
        api_key, project = await authenticate_api_key(plaintext, db)
        return MachinePrincipal(
            cast(str, api_key.public_id),
            cast(int, project.id),
            frozenset(api_key.scopes),
        )
    return await get_current_active_user(await get_current_user(token, db))


def management_actor(user: User | MachinePrincipal) -> int | MachinePrincipal:
    return user if isinstance(user, MachinePrincipal) else cast(int, user.id)
