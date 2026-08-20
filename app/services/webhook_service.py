from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.admission import (
    AdmissionController,
    endpoint_quota_values,
    tenant_quota_values,
)
from app.config import Settings
from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.models import (
    ApiKey,
    Delivery,
    EndpointQuotaState,
    Event,
    Organization,
    OrganizationMember,
    Project,
    ReplayOperation,
    TenantQuotaState,
    User,
    WebhookEndpoint,
)
from app.security_observability import (
    SecurityLayer,
    record_security_deny,
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

    async def _project(
        self, user_id: int, public_id: str, owner_only: bool = False
    ) -> Project:
        query = (
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
        if owner_only:
            query = query.where(OrganizationMember.role == "owner")
        project = await self.db.scalar(query)
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
            TenantQuotaState(
                **tenant_quota_values(
                    organization.id, self.settings, now
                )
            )
        )
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
            record_security_deny(SecurityLayer.ADMISSION, exc.reason)
            raise ValidationError(str(exc), "url") from exc
        now = await AdmissionController(
            self.db, self.settings
        ).check_endpoint_limit(project.organization_id, project.id)
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
        await self.db.flush()
        self.db.add(
            EndpointQuotaState(
                **endpoint_quota_values(endpoint.id, self.settings, now)
            )
        )
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
                record_security_deny(SecurityLayer.ADMISSION, exc.reason)
                raise ValidationError(str(exc), "url") from exc
        if changes.get("is_active") is True and not endpoint.is_active:
            await AdmissionController(
                self.db, self.settings
            ).check_endpoint_limit(
                project.organization_id,
                project.id,
                exclude_endpoint_id=endpoint.id,
            )
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
        controller = AdmissionController(self.db, self.settings)
        _, tenant_state, now = await controller.lock_global_tenant(
            project.organization_id
        )
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
        event_public_id = str(uuid4())
        canonical_envelope = canonical_json(
            {
                "id": event_public_id,
                "type": event_type,
                "created_at": now.isoformat(),
                "data": payload,
            }
        )
        endpoints = list(
            await self.db.scalars(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.project_id == project_id,
                    WebhookEndpoint.is_active.is_(True),
                )
            )
        )
        await controller.admit_event_locked(
            tenant_state,
            now,
            project.organization_id,
            len(endpoints),
            len(canonical_envelope),
        )
        event = Event(
            public_id=event_public_id,
            project_id=project_id,
            idempotency_key=idempotency_key,
            event_type=event_type,
            payload=payload,
            payload_hash=fingerprint,
            canonical_envelope=canonical_envelope,
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
        for endpoint in endpoints:
            self.db.add(
                Delivery(
                    public_id=str(uuid4()),
                    organization_id=project.organization_id,
                    event_id=event.id,
                    endpoint_id=endpoint.id,
                    endpoint_public_id_snapshot=endpoint.public_id,
                    endpoint_url_snapshot=endpoint.url,
                    endpoint_active_snapshot=endpoint.is_active,
                    signing_secret_version_snapshot=endpoint.secret_version,
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
        project = await self._project(user_id, project_id)
        original = await self.get_delivery(user_id, project_id, delivery_id)
        now = await AdmissionController(
            self.db, self.settings
        ).admit_replay(original.organization_id)
        replay = self._replay_copy(original, now)
        self.db.add(replay)
        self.db.add(
            ReplayOperation(
                public_id=str(uuid4()),
                organization_id=original.organization_id,
                project_id=project.id,
                actor_user_id=user_id,
                idempotency_key=f"legacy-{uuid4()}",
                mode="single",
                requested_count=1,
                created_count=1,
                source_delivery_ids=[original.public_id],
                created_delivery_ids=[replay.public_id],
                created_at=now,
            )
        )
        await self.db.commit()
        await self.db.refresh(replay)
        return replay

    @staticmethod
    def _replay_copy(original: Delivery, now: datetime) -> Delivery:
        return Delivery(
            public_id=str(uuid4()),
            organization_id=original.organization_id,
            event_id=original.event_id,
            endpoint_id=original.endpoint_id,
            replay_of_delivery_id=original.id,
            endpoint_public_id_snapshot=(
                original.endpoint_public_id_snapshot
            ),
            endpoint_url_snapshot=original.endpoint_url_snapshot,
            endpoint_active_snapshot=original.endpoint_active_snapshot,
            signing_secret_version_snapshot=(
                original.signing_secret_version_snapshot
            ),
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )

    async def replay_deliveries(
        self,
        user_id: int,
        project_id: str,
        delivery_ids: list[str],
        idempotency_key: str,
    ) -> ReplayOperation:
        project = await self._project(user_id, project_id)
        if not (
            1
            <= len(idempotency_key)
            <= self.settings.idempotency_key_max_length
        ):
            raise ValidationError("Idempotency-Key length is invalid")
        if not (
            1
            <= len(delivery_ids)
            <= self.settings.bulk_replay_max_deliveries
        ):
            raise ValidationError("Replay batch size exceeds configured limit")
        existing = await self.db.scalar(
            select(ReplayOperation).where(
                ReplayOperation.project_id == project.id,
                ReplayOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.source_delivery_ids != delivery_ids:
                raise ConflictError(
                    "Idempotency-Key was already used with "
                    "different deliveries"
                )
            return existing

        controller = AdmissionController(self.db, self.settings)
        _, tenant_state, now = await controller.lock_global_tenant(
            project.organization_id
        )
        existing = await self.db.scalar(
            select(ReplayOperation).where(
                ReplayOperation.project_id == project.id,
                ReplayOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.source_delivery_ids != delivery_ids:
                raise ConflictError(
                    "Idempotency-Key was already used with "
                    "different deliveries"
                )
            return existing
        sources = list(
            await self.db.scalars(
                select(Delivery)
                .join(Event)
                .where(
                    Event.project_id == project.id,
                    Delivery.public_id.in_(delivery_ids),
                    Delivery.status == "dead",
                )
                .with_for_update(of=Delivery)
            )
        )
        by_public_id = {delivery.public_id: delivery for delivery in sources}
        if len(by_public_id) != len(delivery_ids):
            raise ValidationError(
                "All replay sources must be dead deliveries in the project"
            )
        await controller.admit_replays_locked(
            tenant_state, now, len(delivery_ids)
        )
        replays = [
            self._replay_copy(by_public_id[public_id], now)
            for public_id in delivery_ids
        ]
        self.db.add_all(replays)
        operation = ReplayOperation(
            public_id=str(uuid4()),
            organization_id=project.organization_id,
            project_id=project.id,
            actor_user_id=user_id,
            idempotency_key=idempotency_key,
            mode="single" if len(delivery_ids) == 1 else "bulk",
            requested_count=len(delivery_ids),
            created_count=len(replays),
            source_delivery_ids=delivery_ids,
            created_delivery_ids=[replay.public_id for replay in replays],
            created_at=now,
        )
        self.db.add(operation)
        await self.db.commit()
        await self.db.refresh(operation)
        return operation

    async def list_dead_deliveries(
        self,
        user_id: int,
        project_id: str,
        offset: int,
        limit: int,
        endpoint_id: str | None = None,
        reason: str | None = None,
        minimum_age_seconds: int | None = None,
    ) -> list[Delivery]:
        project = await self._project(user_id, project_id)
        query = (
            select(Delivery)
            .join(Event)
            .where(Event.project_id == project.id, Delivery.status == "dead")
        )
        if endpoint_id is not None:
            query = query.where(
                Delivery.endpoint_public_id_snapshot == endpoint_id
            )
        if reason is not None:
            query = query.where(Delivery.dead_reason == reason)
        if minimum_age_seconds is not None:
            cutoff = utcnow() - timedelta(seconds=minimum_age_seconds)
            query = query.where(Delivery.dead_at <= cutoff)
        result = await self.db.scalars(
            query.order_by(Delivery.dead_at.desc(), Delivery.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def _endpoint_runtime(
        self,
        user_id: int,
        project_id: str,
        endpoint_id: str,
        lock: bool = False,
    ) -> tuple[WebhookEndpoint, EndpointQuotaState]:
        project = await self._project(user_id, project_id)
        endpoint_query = select(WebhookEndpoint).where(
            WebhookEndpoint.project_id == project.id,
            WebhookEndpoint.public_id == endpoint_id,
        )
        if lock:
            endpoint_query = endpoint_query.with_for_update()
        endpoint = await self.db.scalar(endpoint_query)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint", endpoint_id)
        state_query = select(EndpointQuotaState).where(
            EndpointQuotaState.endpoint_id == endpoint.id
        )
        if lock:
            state_query = state_query.with_for_update()
        state = await self.db.scalar(state_query)
        if state is None:
            raise RuntimeError("Endpoint runtime state is unavailable")
        return endpoint, state

    @staticmethod
    def _runtime_view(
        endpoint: WebhookEndpoint, state: EndpointQuotaState
    ) -> dict[str, object]:
        return {
            "endpoint_id": endpoint.public_id,
            "paused": state.paused_at is not None,
            "pause_reason": state.pause_reason,
            "circuit_state": state.circuit_state,
            "consecutive_failures": state.consecutive_failures,
            "circuit_open_until": state.circuit_open_until,
        }

    async def pause_endpoint(
        self,
        user_id: int,
        project_id: str,
        endpoint_id: str,
        reason: str | None,
    ) -> dict[str, object]:
        endpoint, state = await self._endpoint_runtime(
            user_id, project_id, endpoint_id, lock=True
        )
        now = utcnow()
        state.paused_at = state.paused_at or now
        state.pause_reason = reason
        state.updated_at = now
        await self.db.commit()
        return self._runtime_view(endpoint, state)

    async def resume_endpoint(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> dict[str, object]:
        endpoint, state = await self._endpoint_runtime(
            user_id, project_id, endpoint_id, lock=True
        )
        state.paused_at = None
        state.pause_reason = None
        state.updated_at = utcnow()
        await self.db.commit()
        return self._runtime_view(endpoint, state)

    async def recover_endpoint_circuit(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> dict[str, object]:
        endpoint, state = await self._endpoint_runtime(
            user_id, project_id, endpoint_id, lock=True
        )
        now = utcnow()
        state.retry_tokens = float(self.settings.endpoint_retry_burst)
        state.retry_refilled_at = now
        state.circuit_state = "closed"
        state.consecutive_failures = 0
        state.circuit_open_until = None
        state.half_open_probe_delivery_id = None
        state.updated_at = now
        await self.db.commit()
        return self._runtime_view(endpoint, state)

    async def cancel_delivery(
        self,
        user_id: int,
        project_id: str,
        delivery_id: str,
        reason: str | None,
    ) -> Delivery:
        project = await self._project(user_id, project_id)
        delivery = await self.db.scalar(
            select(Delivery)
            .join(Event)
            .where(
                Event.project_id == project.id,
                Delivery.public_id == delivery_id,
            )
            .with_for_update(of=Delivery)
        )
        if delivery is None:
            raise NotFoundError("Delivery", delivery_id)
        if delivery.status == "canceled":
            return delivery
        if delivery.status not in {"pending", "retry_scheduled"}:
            raise ConflictError(
                "Only pending or retry-scheduled deliveries can be canceled"
            )
        now = utcnow()
        delivery.status = "canceled"
        delivery.canceled_at = now
        delivery.canceled_reason = reason
        delivery.next_attempt_at = now
        delivery.updated_at = now
        state = await self.db.scalar(
            select(EndpointQuotaState)
            .where(EndpointQuotaState.endpoint_id == delivery.endpoint_id)
            .with_for_update()
        )
        if (
            state is not None
            and state.half_open_probe_delivery_id == delivery.id
        ):
            state.half_open_probe_delivery_id = None
            state.circuit_state = "open"
            state.circuit_open_until = now
            state.updated_at = now
        await self.db.commit()
        await self.db.refresh(delivery)
        return delivery

    async def purge_terminal_deliveries(
        self,
        user_id: int,
        project_id: str,
        dry_run: bool,
        max_records: int,
    ) -> dict[str, object]:
        project = await self._project(
            user_id, project_id, owner_only=True
        )
        limit = min(max_records, self.settings.delivery_purge_batch_size)
        cutoff = utcnow() - timedelta(
            days=self.settings.delivery_retention_days
        )
        eligible = (
            select(Delivery.id)
            .join(Event)
            .where(
                Event.project_id == project.id,
                Delivery.status.in_(("succeeded", "dead", "canceled")),
                Delivery.updated_at <= cutoff,
            )
            .order_by(Delivery.id)
            .limit(limit)
        )
        delivery_ids = list((await self.db.scalars(eligible)))
        if not dry_run and delivery_ids:
            await self.db.execute(
                delete(Delivery).where(Delivery.id.in_(delivery_ids))
            )
            await self.db.commit()
        return {
            "cutoff": cutoff,
            "matched": len(delivery_ids),
            "purged": 0 if dry_run else len(delivery_ids),
            "dry_run": dry_run,
        }
