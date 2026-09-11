"""Append-only administrative audit helpers with metadata safeguards."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TypeAlias
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdministrativeAuditEvent

AuditScalar: TypeAlias = str | int | float | bool | None
AuditValue: TypeAlias = AuditScalar | list[AuditScalar]

_FORBIDDEN_KEY_PARTS = (
    "secret",
    "token",
    "key",
    "digest",
    "payload",
    "body",
    "url",
    "authorization",
)
_MAX_METADATA_KEYS = 64
_MAX_LIST_VALUES = 64
_MAX_TOTAL_VALUES = 256
_MAX_KEY_LENGTH = 100
_MAX_STRING_LENGTH = 2_048
_CREDENTIAL_VALUE = re.compile(r"\A[A-Za-z0-9_+/=-]{40,}\Z")
_JWT_VALUE = re.compile(r"\A[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
_SENSITIVE_VALUE_PARTS = (
    "authorization:",
    "bearer ",
    "basic ",
    "-----begin ",
    "http://",
    "https://",
    "password",
    "secret",
)


def _normalize_key(key: str) -> str:
    return "".join(
        character for character in key.lower() if character.isalnum()
    )


def _sanitize_scalar(value: object) -> AuditScalar:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Audit metadata numbers must be finite")
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if len(value) > _MAX_STRING_LENGTH:
            raise ValueError("Audit metadata strings are too long")
        if any(part in lowered for part in _SENSITIVE_VALUE_PARTS):
            raise ValueError("Audit metadata contains sensitive content")
        if _JWT_VALUE.fullmatch(value) or _CREDENTIAL_VALUE.fullmatch(value):
            raise ValueError("Audit metadata contains a possible credential")
        return value
    raise ValueError("Audit metadata values must be scalar or scalar lists")


def sanitize_audit_metadata(
    metadata: Mapping[str, object] | None,
) -> dict[str, AuditValue]:
    """Return a bounded JSON-safe copy without nested structures."""
    if metadata is None:
        return {}
    if len(metadata) > _MAX_METADATA_KEYS:
        raise ValueError("Audit metadata contains too many keys")

    sanitized: dict[str, AuditValue] = {}
    total_values = 0
    for key, value in metadata.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_KEY_LENGTH:
            raise ValueError("Audit metadata keys are invalid")
        normalized_key = _normalize_key(key)
        if any(part in normalized_key for part in _FORBIDDEN_KEY_PARTS):
            raise ValueError("Audit metadata key may contain sensitive data")

        if isinstance(value, list):
            if len(value) > _MAX_LIST_VALUES:
                raise ValueError("Audit metadata lists are too long")
            total_values += len(value)
            sanitized[key] = [_sanitize_scalar(item) for item in value]
        else:
            total_values += 1
            sanitized[key] = _sanitize_scalar(value)
        if total_values > _MAX_TOTAL_VALUES:
            raise ValueError("Audit metadata contains too many values")
    return sanitized


async def append_audit(
    session: AsyncSession,
    actor_user_id: int | None,
    organization_public_id: str,
    action: str,
    resource_type: str,
    resource_public_id: str | None = None,
    project_public_id: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> AdministrativeAuditEvent:
    """Add an audit event to the caller's transaction without committing."""
    if not organization_public_id or len(organization_public_id) > 36:
        raise ValueError("Organization public ID is invalid")
    if project_public_id is not None and len(project_public_id) > 36:
        raise ValueError("Project public ID is invalid")
    if not action or len(action) > 100:
        raise ValueError("Audit action is invalid")
    if not resource_type or len(resource_type) > 100:
        raise ValueError("Audit resource type is invalid")
    if resource_public_id is not None and len(resource_public_id) > 36:
        raise ValueError("Resource public ID is invalid")

    event = AdministrativeAuditEvent(
        public_id=str(uuid4()),
        organization_public_id=organization_public_id,
        project_public_id=project_public_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_public_id=resource_public_id,
        sanitized_metadata=sanitize_audit_metadata(metadata),
        created_at=datetime.now(timezone.utc),
    )
    session.add(event)
    return event


async def list_audit(
    session: AsyncSession,
    organization_public_id: str,
    *,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 100,
    before_created_at: datetime | None = None,
    before_id: int | None = None,
) -> list[AdministrativeAuditEvent]:
    """List bounded organization history; authorization is caller-owned."""
    if not organization_public_id:
        raise ValueError("Organization public ID is required")
    if not 1 <= limit <= 1_000:
        raise ValueError("Audit limit must be between 1 and 1000")
    if before_id is not None and before_created_at is None:
        raise ValueError("Audit cursor timestamp is required with cursor ID")

    statement = select(AdministrativeAuditEvent).where(
        AdministrativeAuditEvent.organization_public_id
        == organization_public_id
    )
    if action is not None:
        statement = statement.where(AdministrativeAuditEvent.action == action)
    if resource_type is not None:
        statement = statement.where(
            AdministrativeAuditEvent.resource_type == resource_type
        )
    if before_created_at is not None:
        cursor_id = before_id if before_id is not None else 0
        statement = statement.where(
            or_(
                AdministrativeAuditEvent.created_at < before_created_at,
                (AdministrativeAuditEvent.created_at == before_created_at)
                & (AdministrativeAuditEvent.id < cursor_id),
            )
        )

    rows = await session.scalars(
        statement.order_by(
            AdministrativeAuditEvent.created_at.desc(),
            AdministrativeAuditEvent.id.desc(),
        ).limit(limit)
    )
    return list(rows)
