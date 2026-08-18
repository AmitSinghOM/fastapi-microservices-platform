from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import (
    ApiKey,
    Delivery,
    Event,
    Organization,
    OrganizationMember,
    Project,
    User,
    WebhookEndpoint,
)
from app.webhook_security import (
    UnsafeWebhookUrl,
    canonical_json,
    digest_api_key,
    endpoint_secret,
    generate_api_key,
    validate_webhook_url,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WebhookService:
    """Tenant-scoped webhook control-plane operations."""

    def __init__(self, db: AsyncSession, settings: Settings):
        self.db = db
        self.settings = settings

    async def _organization(
        self, user_id: int, public_id: str, owner_only: bool = False
    ) -> Organization:
        query = (
            select(Organization)
            .join(OrganizationMember)
            .where(
                Organization.public_id == public_id,
                OrganizationMember.user_id == user_id,
            )
        )
        if owner_only:
            query = query.where(OrganizationMember.role == "owner")
        organization = await self.db.scalar(query)
        if organization is None:
            raise NotFoundError("Organization", public_id)
        return organization

    async def _project(self, user_id: int, public_id: str) -> Project:
        project = await self.db.scalar(
            select(Project)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id
                == Project.organization_id,
            )
            .where(
                Project.public_id == public_id,
                OrganizationMember.user_id == user_id,
            )
        )
        if project is None:
            raise NotFoundError("Project", public_id)
        return project

    async def create_organization(
        self, user_id: int, name: str
    ) -> Organization:
        now = utcnow()
        organization = Organization(
            public_id=str(uuid4()), name=name, created_at=now
        )
        self.db.add(organization)
        await self.db.flush()
        self.db.add(
            OrganizationMember(
                organization_id=organization.id,
                user_id=user_id,
                role="owner",
                created_at=now,
            )
        )
        await self.db.commit()
        await self.db.refresh(organization)
        return organization

    async def list_organizations(
        self, user_id: int, offset: int, limit: int
    ) -> list[Organization]:
        result = await self.db.scalars(
            select(Organization)
            .join(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
            .order_by(Organization.created_at.desc(), Organization.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def add_member(
        self,
        user_id: int,
        organization_id: str,
        member_user_id: int,
        role: str,
    ) -> OrganizationMember:
        organization = await self._organization(
            user_id, organization_id, owner_only=True
        )
        if await self.db.get(User, member_user_id) is None:
            raise NotFoundError("User", member_user_id)
        existing = await self.db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == member_user_id,
            )
        )
        if existing:
            raise ConflictError("User is already an organization member")
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=member_user_id,
            role=role,
            created_at=utcnow(),
        )
        self.db.add(member)
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def list_members(
        self, user_id: int, organization_id: str, offset: int, limit: int
    ) -> list[OrganizationMember]:
        organization = await self._organization(user_id, organization_id)
        result = await self.db.scalars(
            select(OrganizationMember)
            .where(OrganizationMember.organization_id == organization.id)
            .order_by(OrganizationMember.created_at, OrganizationMember.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def create_project(
        self, user_id: int, organization_id: str, name: str
    ) -> Project:
        organization = await self._organization(user_id, organization_id)
        project = Project(
            public_id=str(uuid4()),
            organization_id=organization.id,
            name=name,
            is_active=True,
            created_at=utcnow(),
        )
        self.db.add(project)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise ConflictError("Project name already exists") from exc
        await self.db.refresh(project)
        return project

    async def list_projects(
        self, user_id: int, organization_id: str, offset: int, limit: int
    ) -> list[Project]:
        organization = await self._organization(user_id, organization_id)
        result = await self.db.scalars(
            select(Project)
            .where(Project.organization_id == organization.id)
            .order_by(Project.created_at.desc(), Project.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def deactivate_project(
        self, user_id: int, project_id: str
    ) -> Project:
        project = await self._project(user_id, project_id)
        project.is_active = False
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def create_api_key(
        self, user_id: int, project_id: str, name: str
    ) -> tuple[ApiKey, str]:
        project = await self._project(user_id, project_id)
        plaintext, prefix = generate_api_key()
        api_key = ApiKey(
            public_id=str(uuid4()),
            project_id=project.id,
            name=name,
            key_prefix=prefix,
            key_digest=digest_api_key(plaintext, self.settings.api_key_pepper),
            is_active=True,
            created_at=utcnow(),
        )
        self.db.add(api_key)
        await self.db.commit()
        await self.db.refresh(api_key)
        return api_key, plaintext

    async def list_api_keys(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[ApiKey]:
        project = await self._project(user_id, project_id)
        result = await self.db.scalars(
            select(ApiKey)
            .where(ApiKey.project_id == project.id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def revoke_api_key(
        self, user_id: int, project_id: str, key_id: str
    ) -> ApiKey:
        project = await self._project(user_id, project_id)
        api_key = await self.db.scalar(
            select(ApiKey).where(
                ApiKey.project_id == project.id, ApiKey.public_id == key_id
            )
        )
        if api_key is None:
            raise NotFoundError("API key", key_id)
        api_key.is_active = False
        api_key.revoked_at = utcnow()
        await self.db.commit()
        await self.db.refresh(api_key)
        return api_key

    async def create_endpoint(
        self, user_id: int, project_id: str, url: str, description: str | None
    ) -> tuple[WebhookEndpoint, str]:
        project = await self._project(user_id, project_id)
        try:
            await validate_webhook_url(
                url, bool(self.settings.allow_http_webhooks)
            )
        except UnsafeWebhookUrl as exc:
            raise ValidationError(str(exc), "url") from exc
        now = utcnow()
        endpoint = WebhookEndpoint(
            public_id=str(uuid4()),
            project_id=project.id,
            url=url,
            description=description,
            is_active=True,
            secret_version=1,
            created_at=now,
            updated_at=now,
        )
        self.db.add(endpoint)
        await self.db.commit()
        await self.db.refresh(endpoint)
        return endpoint, endpoint_secret(
            self.settings.webhook_signing_key,
            endpoint.public_id,
            endpoint.secret_version,
        )

    async def list_endpoints(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[WebhookEndpoint]:
        project = await self._project(user_id, project_id)
        result = await self.db.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.project_id == project.id)
            .order_by(
                WebhookEndpoint.created_at.desc(), WebhookEndpoint.id.desc()
            )
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def update_endpoint(
        self,
        user_id: int,
        project_id: str,
        endpoint_id: str,
        changes: dict,
    ) -> WebhookEndpoint:
        project = await self._project(user_id, project_id)
        endpoint = await self.db.scalar(
            select(WebhookEndpoint).where(
                WebhookEndpoint.project_id == project.id,
                WebhookEndpoint.public_id == endpoint_id,
            )
        )
        if endpoint is None:
            raise NotFoundError("Webhook endpoint", endpoint_id)
        if changes.get("url") is not None:
            try:
                await validate_webhook_url(
                    changes["url"], bool(self.settings.allow_http_webhooks)
                )
            except UnsafeWebhookUrl as exc:
                raise ValidationError(str(exc), "url") from exc
        for field, value in changes.items():
            setattr(endpoint, field, value)
        endpoint.updated_at = utcnow()
        await self.db.commit()
        await self.db.refresh(endpoint)
        return endpoint

    async def rotate_endpoint_secret(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> tuple[WebhookEndpoint, str]:
        endpoint = await self.update_endpoint(
            user_id, project_id, endpoint_id, {}
        )
        endpoint.secret_version += 1
        endpoint.updated_at = utcnow()
        await self.db.commit()
        await self.db.refresh(endpoint)
        secret = endpoint_secret(
            self.settings.webhook_signing_key,
            endpoint.public_id,
            endpoint.secret_version,
        )
        return endpoint, secret

    async def ingest_event(
        self,
        project: Project,
        idempotency_key: str,
        event_type: str,
        payload: object,
    ) -> Event:
        project_id = project.id
        key_max_length = self.settings.idempotency_key_max_length
        if not 1 <= len(idempotency_key) <= key_max_length:
            raise ValidationError("Idempotency-Key length is invalid")
        try:
            payload_bytes = canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Payload must be valid finite JSON") from exc
        if len(payload_bytes) > self.settings.webhook_payload_max_bytes:
            raise ValidationError("Webhook payload exceeds configured limit")
        fingerprint = hashlib.sha256(
            event_type.encode("utf-8") + b"\0" + payload_bytes
        ).hexdigest()
        existing = await self.db.scalar(
            select(Event).where(
                Event.project_id == project_id,
                Event.idempotency_key == idempotency_key,
            )
        )
        if existing:
            content_changed = (
                existing.event_type != event_type
                or existing.payload_hash != fingerprint
            )
            if content_changed:
                raise ConflictError(
                    "Idempotency-Key was already used with different content"
                )
            return existing
        now = utcnow()
        event = Event(
            public_id=str(uuid4()),
            project_id=project_id,
            idempotency_key=idempotency_key,
            event_type=event_type,
            payload=payload,
            payload_hash=fingerprint,
            created_at=now,
        )
        self.db.add(event)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raced = await self.db.scalar(
                select(Event).where(
                    Event.project_id == project_id,
                    Event.idempotency_key == idempotency_key,
                )
            )
            raced_matches = (
                raced
                and raced.event_type == event_type
                and raced.payload_hash == fingerprint
            )
            if raced_matches:
                return raced
            raise ConflictError(
                "Idempotency-Key was already used with different content"
            )
        endpoints = await self.db.scalars(
            select(WebhookEndpoint).where(
                WebhookEndpoint.project_id == project_id,
                WebhookEndpoint.is_active.is_(True),
            )
        )
        for endpoint in endpoints:
            self.db.add(
                Delivery(
                    public_id=str(uuid4()),
                    event_id=event.id,
                    endpoint_id=endpoint.id,
                    status="pending",
                    attempt_count=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self.db.commit()
        await self.db.refresh(event)
        return event

    async def list_events(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[Event]:
        project = await self._project(user_id, project_id)
        result = await self.db.scalars(
            select(Event)
            .where(Event.project_id == project.id)
            .order_by(Event.created_at.desc(), Event.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def get_event(
        self, user_id: int, project_id: str, event_id: str
    ) -> Event:
        project = await self._project(user_id, project_id)
        event = await self.db.scalar(
            select(Event).where(
                Event.project_id == project.id, Event.public_id == event_id
            )
        )
        if event is None:
            raise NotFoundError("Event", event_id)
        return event

    async def list_deliveries(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[Delivery]:
        project = await self._project(user_id, project_id)
        result = await self.db.scalars(
            select(Delivery)
            .join(Event)
            .where(Event.project_id == project.id)
            .order_by(Delivery.created_at.desc(), Delivery.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def get_delivery(
        self, user_id: int, project_id: str, delivery_id: str
    ) -> Delivery:
        project = await self._project(user_id, project_id)
        delivery = await self.db.scalar(
            select(Delivery)
            .join(Event)
            .options(selectinload(Delivery.attempts))
            .where(
                Event.project_id == project.id,
                Delivery.public_id == delivery_id,
            )
        )
        if delivery is None:
            raise NotFoundError("Delivery", delivery_id)
        return delivery

    async def replay_delivery(
        self, user_id: int, project_id: str, delivery_id: str
    ) -> Delivery:
        original = await self.get_delivery(user_id, project_id, delivery_id)
        now = utcnow()
        replay = Delivery(
            public_id=str(uuid4()),
            event_id=original.event_id,
            endpoint_id=original.endpoint_id,
            replay_of_delivery_id=original.id,
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        self.db.add(replay)
        await self.db.commit()
        await self.db.refresh(replay)
        return replay
