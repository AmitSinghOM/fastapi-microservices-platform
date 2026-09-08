from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import Select

from app.admission import (
    AdmissionController,
    database_now,
    endpoint_quota_values,
    tenant_quota_values,
)
from app.audit import append_audit, list_audit
from app.authorization import (
    Permission,
    authorize_organization,
    authorize_project,
)
from app.config import Settings
from app.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.models import (
    ApiKey,
    Delivery,
    DeliveryAttempt,
    EndpointQuotaState,
    EndpointSigningSecretVersion,
    Event,
    Organization,
    OrganizationLifecycleOperation,
    OrganizationMember,
    OrganizationPolicy,
    Project,
    ProjectMember,
    ReplayOperation,
    TenantQuotaState,
    User,
    WebhookEndpoint,
)
from app.observability import (
    current_trace_headers,
    record_enqueue,
    record_event,
)
from app.security_observability import SecurityLayer, record_security_deny
from app.webhook_security import (
    UnsafeWebhookUrl,
    canonical_json,
    digest_api_key,
    endpoint_secret,
    generate_api_key,
    validate_webhook_url,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class WebhookService:
    """Tenant-scoped webhook data-plane and management operations."""

    def __init__(self, db: AsyncSession, settings: Settings):
        self.db = db
        self.settings = settings

    async def _audit(
        self,
        user_id: int,
        organization_id: str,
        action: str,
        resource_type: str,
        now: datetime,
        *,
        resource_id: str | None = None,
        project_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        event = await append_audit(
            self.db,
            user_id,
            organization_id,
            action,
            resource_type,
            resource_id,
            project_id,
            metadata,
        )
        event.created_at = now

    async def _organization_role(
        self, organization_id: int, user_id: int
    ) -> str:
        role = await self.db.scalar(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
        )
        if role is None:
            raise NotFoundError("Organization member", user_id)
        return str(role)

    async def _require_owner_for_owner_change(
        self,
        organization: Organization,
        user_id: int,
        current_role: str | None,
        requested_role: str | None,
    ) -> None:
        if "owner" not in {current_role, requested_role}:
            return
        if await self._organization_role(organization.id, user_id) != "owner":
            raise ForbiddenError("Only an owner can change owner membership")

    async def _ensure_not_last_owner(
        self, organization_id: int, member: OrganizationMember
    ) -> None:
        if member.role != "owner":
            return
        owners = list(
            await self.db.scalars(
                select(OrganizationMember)
                .where(
                    OrganizationMember.organization_id == organization_id,
                    OrganizationMember.role == "owner",
                )
                .with_for_update()
            )
        )
        if len(owners) <= 1:
            raise ConflictError("An organization must retain an owner")

    async def create_organization(
        self, user_id: int, name: str
    ) -> Organization:
        now = await database_now(self.db)
        organization = Organization(
            public_id=str(uuid4()),
            name=name,
            lifecycle_state="active",
            created_at=now,
        )
        self.db.add(organization)
        await self.db.flush()
        self.db.add_all(
            (
                TenantQuotaState(
                    **tenant_quota_values(organization.id, self.settings, now)
                ),
                OrganizationPolicy(
                    organization_id=organization.id,
                    plan="free",
                    payload_retention_days=(
                        self.settings.default_payload_retention_days
                    ),
                    response_retention_days=(
                        self.settings.default_response_retention_days
                    ),
                    created_at=now,
                    updated_at=now,
                ),
                OrganizationMember(
                    organization_id=organization.id,
                    user_id=user_id,
                    role="owner",
                    created_at=now,
                ),
            )
        )
        await self.db.flush()
        await authorize_organization(
            self.db, user_id, organization.public_id, Permission.ORG_READ
        )
        await self._audit(
            user_id,
            organization.public_id,
            "organization.created",
            "organization",
            now,
            resource_id=organization.public_id,
        )
        await self.db.commit()
        await self.db.refresh(organization)
        return organization

    async def list_organizations(
        self, user_id: int, offset: int, limit: int
    ) -> list[Organization]:
        candidates = list(
            await self.db.scalars(
                select(Organization)
                .join(OrganizationMember)
                .where(OrganizationMember.user_id == user_id)
                .order_by(
                    Organization.created_at.desc(), Organization.id.desc()
                )
                .offset(offset)
                .limit(limit)
            )
        )
        return [
            await authorize_organization(
                self.db,
                user_id,
                organization.public_id,
                Permission.ORG_READ,
            )
            for organization in candidates
        ]

    async def add_member(
        self,
        user_id: int,
        organization_id: str,
        member_user_id: int,
        role: str,
    ) -> OrganizationMember:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.MEMBER_MANAGE
        )
        await self._require_owner_for_owner_change(
            organization, user_id, None, role
        )
        if await self.db.get(User, member_user_id) is None:
            raise NotFoundError("User", member_user_id)
        existing = await self.db.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == member_user_id,
            )
        )
        if existing is not None:
            raise ConflictError("User is already an organization member")
        now = await database_now(self.db)
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=member_user_id,
            role=role,
            created_at=now,
        )
        self.db.add(member)
        await self._audit(
            user_id,
            organization.public_id,
            "organization.member_added",
            "organization_member",
            now,
            metadata={"member_user_id": member_user_id, "role": role},
        )
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def list_members(
        self, user_id: int, organization_id: str, offset: int, limit: int
    ) -> list[OrganizationMember]:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.ORG_READ
        )
        result = await self.db.scalars(
            select(OrganizationMember)
            .where(OrganizationMember.organization_id == organization.id)
            .order_by(OrganizationMember.created_at, OrganizationMember.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def update_member(
        self,
        user_id: int,
        organization_id: str,
        member_user_id: int,
        role: str,
    ) -> OrganizationMember:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.MEMBER_MANAGE
        )
        member = await self.db.scalar(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == member_user_id,
            )
            .with_for_update()
        )
        if member is None:
            raise NotFoundError("Organization member", member_user_id)
        await self._require_owner_for_owner_change(
            organization, user_id, str(member.role), role
        )
        if member.role == "owner" and role != "owner":
            await self._ensure_not_last_owner(organization.id, member)
        previous_role = str(member.role)
        now = await database_now(self.db)
        member.role = role
        await self._audit(
            user_id,
            organization.public_id,
            "organization.member_updated",
            "organization_member",
            now,
            metadata={
                "member_user_id": member_user_id,
                "previous_role": previous_role,
                "current_role": role,
            },
        )
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def remove_member(
        self, user_id: int, organization_id: str, member_user_id: int
    ) -> None:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.MEMBER_MANAGE
        )
        member = await self.db.scalar(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == member_user_id,
            )
            .with_for_update()
        )
        if member is None:
            raise NotFoundError("Organization member", member_user_id)
        await self._require_owner_for_owner_change(
            organization, user_id, str(member.role), None
        )
        await self._ensure_not_last_owner(organization.id, member)
        now = await database_now(self.db)
        await self.db.execute(
            delete(ProjectMember).where(
                ProjectMember.user_id == member_user_id,
                ProjectMember.project_id.in_(
                    select(Project.id).where(
                        Project.organization_id == organization.id
                    )
                ),
            )
        )
        await self.db.delete(member)
        await self._audit(
            user_id,
            organization.public_id,
            "organization.member_removed",
            "organization_member",
            now,
            metadata={
                "member_user_id": member_user_id,
                "previous_role": str(member.role),
            },
        )
        await self.db.commit()

    async def get_policy(
        self, user_id: int, organization_id: str
    ) -> OrganizationPolicy:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.POLICY_MANAGE
        )
        policy = await self.db.get(OrganizationPolicy, organization.id)
        if policy is None:
            raise RuntimeError("Organization policy is unavailable")
        return policy

    async def update_policy(
        self, user_id: int, organization_id: str, changes: dict[str, object]
    ) -> OrganizationPolicy:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.POLICY_MANAGE
        )
        policy = await self.db.scalar(
            select(OrganizationPolicy)
            .where(OrganizationPolicy.organization_id == organization.id)
            .with_for_update()
        )
        if policy is None:
            raise RuntimeError("Organization policy is unavailable")
        now = await database_now(self.db)
        for field, value in changes.items():
            setattr(policy, field, value)
        policy.updated_at = now
        await self._audit(
            user_id,
            organization.public_id,
            "organization.policy_updated",
            "organization_policy",
            now,
            metadata={"changed_fields": sorted(changes)},
        )
        await self.db.commit()
        await self.db.refresh(policy)
        return policy

    async def list_audit_events(
        self,
        user_id: int,
        organization_id: str,
        *,
        action: str | None,
        resource_type: str | None,
        limit: int,
        before_created_at: datetime | None,
        before_id: int | None,
    ) -> list[object]:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.AUDIT_READ
        )
        if before_id is not None and before_created_at is None:
            raise ValidationError(
                "Audit cursor timestamp is required with cursor ID"
            )
        return await list_audit(
            self.db,
            organization.public_id,
            action=action,
            resource_type=resource_type,
            limit=limit,
            before_created_at=before_created_at,
            before_id=before_id,
        )

    async def export_organization(
        self, user_id: int, organization_id: str, limit: int
    ) -> dict[str, object]:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.ORG_EXPORT
        )
        now = await database_now(self.db)
        policy = await self.db.get(OrganizationPolicy, organization.id)
        if policy is None:
            raise RuntimeError("Organization policy is unavailable")
        members = list(
            await self.db.scalars(
                select(OrganizationMember)
                .where(OrganizationMember.organization_id == organization.id)
                .order_by(OrganizationMember.id)
                .limit(limit + 1)
            )
        )
        projects = list(
            await self.db.scalars(
                select(Project)
                .where(Project.organization_id == organization.id)
                .order_by(Project.id)
                .limit(limit + 1)
            )
        )
        api_key_rows = list(
            (
                await self.db.execute(
                    select(ApiKey, Project.public_id)
                    .join(Project, Project.id == ApiKey.project_id)
                    .where(Project.organization_id == organization.id)
                    .order_by(ApiKey.id)
                    .limit(limit + 1)
                )
            ).all()
        )
        endpoint_rows = list(
            (
                await self.db.execute(
                    select(WebhookEndpoint, Project.public_id)
                    .join(Project, Project.id == WebhookEndpoint.project_id)
                    .where(Project.organization_id == organization.id)
                    .order_by(WebhookEndpoint.id)
                    .limit(limit + 1)
                )
            ).all()
        )
        lifecycle = await self.db.scalar(
            select(OrganizationLifecycleOperation)
            .where(
                OrganizationLifecycleOperation.organization_id
                == organization.id
            )
            .order_by(
                OrganizationLifecycleOperation.created_at.desc(),
                OrganizationLifecycleOperation.id.desc(),
            )
            .limit(1)
        )
        audit_events = await list_audit(
            self.db, organization.public_id, limit=min(limit + 1, 1_000)
        )

        async def bounded_count(statement: Select[tuple[int]]) -> int:
            bounded = statement.limit(limit + 1).subquery()
            value = await self.db.scalar(
                select(func.count()).select_from(bounded)
            )
            return int(value or 0)

        bounded_counts = {
            "members": await bounded_count(
                select(OrganizationMember.id).where(
                    OrganizationMember.organization_id == organization.id
                )
            ),
            "projects": await bounded_count(
                select(Project.id).where(
                    Project.organization_id == organization.id
                )
            ),
            "project_members": await bounded_count(
                select(ProjectMember.id)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(Project.organization_id == organization.id)
            ),
            "api_keys": await bounded_count(
                select(ApiKey.id)
                .join(Project, Project.id == ApiKey.project_id)
                .where(Project.organization_id == organization.id)
            ),
            "endpoints": await bounded_count(
                select(WebhookEndpoint.id)
                .join(Project, Project.id == WebhookEndpoint.project_id)
                .where(Project.organization_id == organization.id)
            ),
            "events": await bounded_count(
                select(Event.id)
                .join(Project, Project.id == Event.project_id)
                .where(Project.organization_id == organization.id)
            ),
            "deliveries": await bounded_count(
                select(Delivery.id).where(
                    Delivery.organization_id == organization.id
                )
            ),
            "attempts": await bounded_count(
                select(DeliveryAttempt.id)
                .join(Delivery, Delivery.id == DeliveryAttempt.delivery_id)
                .where(Delivery.organization_id == organization.id)
            ),
        }
        truncated = any(
            len(rows) > limit
            for rows in (
                members,
                projects,
                api_key_rows,
                endpoint_rows,
                audit_events,
            )
        ) or any(value > limit for value in bounded_counts.values())

        def api_key_status(api_key: ApiKey) -> str:
            if not api_key.is_active or api_key.revoked_at is not None:
                return "revoked"
            if (
                api_key.expires_at is not None
                and _aware(api_key.expires_at) <= now
            ):
                return "expired"
            return "active"

        return {
            "generated_at": now,
            "organization": organization,
            "policy": policy,
            "lifecycle": lifecycle,
            "counts": {
                name: min(value, limit)
                for name, value in bounded_counts.items()
            },
            "members": members[:limit],
            "projects": [
                {
                    "public_id": project.public_id,
                    "name": project.name,
                    "is_active": project.is_active,
                    "created_at": project.created_at,
                }
                for project in projects[:limit]
            ],
            "api_keys": [
                {
                    "public_id": api_key.public_id,
                    "project_public_id": project_public_id,
                    "prefix": api_key.key_prefix,
                    "scopes": list(api_key.scopes),
                    "expires_at": api_key.expires_at,
                    "status": api_key_status(api_key),
                    "created_at": api_key.created_at,
                }
                for api_key, project_public_id in api_key_rows[:limit]
            ],
            "endpoints": [
                {
                    "public_id": endpoint.public_id,
                    "project_public_id": project_public_id,
                    "description": endpoint.description,
                    "is_active": endpoint.is_active,
                    "signing_version": endpoint.secret_version,
                    "created_at": endpoint.created_at,
                    "updated_at": endpoint.updated_at,
                }
                for endpoint, project_public_id in endpoint_rows[:limit]
            ],
            "audit_events": audit_events[:limit],
            "truncated": truncated,
        }

    async def request_organization_deletion(
        self, user_id: int, organization_id: str
    ) -> OrganizationLifecycleOperation:
        organization = await authorize_organization(
            self.db,
            user_id,
            organization_id,
            Permission.ORG_DELETE,
            allow_deletion_pending=True,
        )
        locked_organization = await self.db.scalar(
            select(Organization)
            .where(Organization.id == organization.id)
            .with_for_update()
        )
        if locked_organization is None:
            raise NotFoundError("Organization", organization_id)
        organization = locked_organization
        existing = await self.db.scalar(
            select(OrganizationLifecycleOperation)
            .where(
                OrganizationLifecycleOperation.organization_id
                == organization.id,
                OrganizationLifecycleOperation.status.in_(
                    ("pending", "running")
                ),
            )
            .with_for_update()
        )
        if existing is not None:
            await self.db.commit()
            return existing
        now = await database_now(self.db)
        scheduled_at = now + timedelta(
            hours=self.settings.organization_deletion_grace_hours
        )
        operation = OrganizationLifecycleOperation(
            public_id=str(uuid4()),
            organization_id=organization.id,
            requested_by_user_id=user_id,
            kind="deletion",
            status="pending",
            scheduled_at=scheduled_at,
            created_at=now,
        )
        organization.lifecycle_state = "deletion_pending"
        organization.deletion_requested_at = now
        organization.deletion_scheduled_at = scheduled_at
        self.db.add(operation)
        await self._audit(
            user_id,
            organization.public_id,
            "organization.deletion_requested",
            "organization",
            now,
            resource_id=organization.public_id,
            metadata={
                "operation_public_id": operation.public_id,
                "scheduled_at": scheduled_at.isoformat(),
            },
        )
        await self.db.commit()
        await self.db.refresh(operation)
        return operation

    async def get_organization_deletion(
        self, user_id: int, organization_id: str
    ) -> OrganizationLifecycleOperation:
        organization = await authorize_organization(
            self.db,
            user_id,
            organization_id,
            Permission.ORG_READ,
        )
        operation = await self.db.scalar(
            select(OrganizationLifecycleOperation)
            .where(
                OrganizationLifecycleOperation.organization_id
                == organization.id
            )
            .order_by(
                OrganizationLifecycleOperation.created_at.desc(),
                OrganizationLifecycleOperation.id.desc(),
            )
            .limit(1)
        )
        if operation is None:
            raise NotFoundError("Organization deletion", organization_id)
        return operation

    async def cancel_organization_deletion(
        self, user_id: int, organization_id: str
    ) -> OrganizationLifecycleOperation:
        organization = await authorize_organization(
            self.db,
            user_id,
            organization_id,
            Permission.ORG_DELETE,
            allow_deletion_pending=True,
        )
        locked_organization = await self.db.scalar(
            select(Organization)
            .where(Organization.id == organization.id)
            .with_for_update()
        )
        if locked_organization is None:
            raise NotFoundError("Organization", organization_id)
        organization = locked_organization
        operation = await self.db.scalar(
            select(OrganizationLifecycleOperation)
            .where(
                OrganizationLifecycleOperation.organization_id
                == organization.id,
                OrganizationLifecycleOperation.status.in_(
                    ("pending", "running")
                ),
            )
            .with_for_update()
        )
        if operation is None:
            raise ConflictError("Organization deletion is not pending")
        if operation.status == "running":
            raise ConflictError("Organization deletion is already running")
        now = await database_now(self.db)
        operation.status = "canceled"
        operation.completed_at = now
        organization.lifecycle_state = "active"
        organization.deletion_requested_at = None
        organization.deletion_scheduled_at = None
        await self._audit(
            user_id,
            organization.public_id,
            "organization.deletion_canceled",
            "organization",
            now,
            resource_id=organization.public_id,
            metadata={"operation_public_id": operation.public_id},
        )
        await self.db.commit()
        await self.db.refresh(operation)
        return operation

    async def create_project(
        self, user_id: int, organization_id: str, name: str
    ) -> Project:
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.PROJECT_MANAGE
        )
        now = await database_now(self.db)
        project = Project(
            public_id=str(uuid4()),
            organization_id=organization.id,
            name=name,
            is_active=True,
            created_at=now,
        )
        self.db.add(project)
        await self.db.flush()
        self.db.add(
            ProjectMember(
                project_id=project.id,
                user_id=user_id,
                role="admin",
                created_at=now,
            )
        )
        await self._audit(
            user_id,
            organization.public_id,
            "project.created",
            "project",
            now,
            resource_id=project.public_id,
            project_id=project.public_id,
        )
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
        organization = await authorize_organization(
            self.db, user_id, organization_id, Permission.ORG_READ
        )
        candidates = list(
            await self.db.scalars(
                select(Project)
                .where(Project.organization_id == organization.id)
                .order_by(Project.created_at.desc(), Project.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        visible: list[Project] = []
        for project in candidates:
            try:
                visible.append(
                    await authorize_project(
                        self.db,
                        user_id,
                        project.public_id,
                        Permission.PROJECT_READ,
                    )
                )
            except NotFoundError:
                continue
        return visible

    async def deactivate_project(
        self, user_id: int, project_id: str
    ) -> Project:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_MANAGE
        )
        now = await database_now(self.db)
        project.is_active = False
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "project.deactivated",
            "project",
            now,
            resource_id=project.public_id,
            project_id=project.public_id,
        )
        await self.db.commit()
        await self.db.refresh(project)
        return project

    async def add_project_member(
        self,
        user_id: int,
        project_id: str,
        member_user_id: int,
        role: str,
    ) -> ProjectMember:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_MANAGE
        )
        organization_member = await self.db.scalar(
            select(OrganizationMember.id).where(
                OrganizationMember.organization_id == project.organization_id,
                OrganizationMember.user_id == member_user_id,
            )
        )
        if organization_member is None:
            raise NotFoundError("Organization member", member_user_id)
        existing = await self.db.scalar(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == member_user_id,
            )
        )
        if existing is not None:
            raise ConflictError("User is already a project member")
        now = await database_now(self.db)
        member = ProjectMember(
            project_id=project.id,
            user_id=member_user_id,
            role=role,
            created_at=now,
        )
        self.db.add(member)
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "project.member_added",
            "project_member",
            now,
            project_id=project.public_id,
            metadata={"member_user_id": member_user_id, "role": role},
        )
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def upsert_project_member(
        self,
        user_id: int,
        project_id: str,
        member_user_id: int,
        role: str,
    ) -> ProjectMember:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_MANAGE
        )
        organization_member = await self.db.scalar(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == project.organization_id,
                OrganizationMember.user_id == member_user_id,
            )
            .with_for_update()
        )
        if organization_member is None:
            raise NotFoundError("Organization member", member_user_id)
        member = await self.db.scalar(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == member_user_id,
            )
            .with_for_update()
        )
        now = await database_now(self.db)
        previous_role: str | None = None
        if member is None:
            member = ProjectMember(
                project_id=project.id,
                user_id=member_user_id,
                role=role,
                created_at=now,
            )
            self.db.add(member)
            action = "project.member_added"
        else:
            previous_role = str(member.role)
            member.role = role
            action = "project.member_updated"
        organization_public_id = await self._project_organization_id(project)
        metadata: dict[str, object] = {
            "member_user_id": member_user_id,
            "role": role,
        }
        if previous_role is not None:
            metadata["previous_role"] = previous_role
        await self._audit(
            user_id,
            organization_public_id,
            action,
            "project_member",
            now,
            project_id=project.public_id,
            metadata=metadata,
        )
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def list_project_members(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[ProjectMember]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_MANAGE
        )
        result = await self.db.scalars(
            select(ProjectMember)
            .where(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.created_at, ProjectMember.id)
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def update_project_member(
        self,
        user_id: int,
        project_id: str,
        member_user_id: int,
        role: str,
    ) -> ProjectMember:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_MANAGE
        )
        member = await self.db.scalar(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == member_user_id,
            )
            .with_for_update()
        )
        if member is None:
            raise NotFoundError("Project member", member_user_id)
        previous_role = str(member.role)
        now = await database_now(self.db)
        member.role = role
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "project.member_updated",
            "project_member",
            now,
            project_id=project.public_id,
            metadata={
                "member_user_id": member_user_id,
                "previous_role": previous_role,
                "current_role": role,
            },
        )
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def remove_project_member(
        self, user_id: int, project_id: str, member_user_id: int
    ) -> None:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_MANAGE
        )
        member = await self.db.scalar(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == member_user_id,
            )
            .with_for_update()
        )
        if member is None:
            raise NotFoundError("Project member", member_user_id)
        now = await database_now(self.db)
        previous_role = str(member.role)
        await self.db.delete(member)
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "project.member_removed",
            "project_member",
            now,
            project_id=project.public_id,
            metadata={
                "member_user_id": member_user_id,
                "previous_role": previous_role,
            },
        )
        await self.db.commit()

    async def _project_organization_id(self, project: Project) -> str:
        organization_id = await self.db.scalar(
            select(Organization.public_id).where(
                Organization.id == project.organization_id
            )
        )
        if organization_id is None:
            raise RuntimeError("Project organization is unavailable")
        return str(organization_id)

    async def _generate_unique_api_key(self) -> tuple[str, str]:
        """Allocate a key whose random 48-bit lookup prefix is unused.

        ``api_keys.key_prefix`` is unique, so an unlucky collision would
        otherwise surface as an unhandled integrity error at commit. The
        check-then-insert window is accepted: a concurrent allocation of
        the same prefix still fails the unique constraint rather than
        corrupting data.
        """
        for _ in range(5):
            plaintext, prefix = generate_api_key()
            exists = await self.db.scalar(
                select(ApiKey.id).where(ApiKey.key_prefix == prefix)
            )
            if exists is None:
                return plaintext, prefix
        raise ConflictError(
            "Could not allocate a unique API key prefix; retry the request"
        )

    def _validate_scopes(self, scopes: list[str]) -> list[str]:
        if not scopes or len(scopes) != len(set(scopes)):
            raise ValidationError(
                "API key scopes must be unique and non-empty"
            )
        if any(scope != "events:write" for scope in scopes):
            raise ValidationError("Unsupported API key scope", "scopes")
        return list(scopes)

    def _api_key_expiry(
        self, now: datetime, expires_in_days: int | None
    ) -> datetime:
        days = (
            self.settings.api_key_default_ttl_days
            if expires_in_days is None
            else expires_in_days
        )
        if not 1 <= days <= 3_650:
            raise ValidationError(
                "API key expiration is invalid", "expires_in_days"
            )
        return now + timedelta(days=days)

    async def create_api_key(
        self,
        user_id: int,
        project_id: str,
        name: str,
        scopes: list[str] | None = None,
        expires_in_days: int | None = None,
    ) -> tuple[ApiKey, str]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.KEY_MANAGE
        )
        now = await database_now(self.db)
        requested_scopes = self._validate_scopes(
            scopes if scopes is not None else ["events:write"]
        )
        plaintext, prefix = await self._generate_unique_api_key()
        public_id = str(uuid4())
        api_key = ApiKey(
            public_id=public_id,
            project_id=project.id,
            name=name,
            key_prefix=prefix,
            key_digest=digest_api_key(plaintext, self.settings.api_key_pepper),
            scopes=requested_scopes,
            expires_at=self._api_key_expiry(now, expires_in_days),
            rotation_family_id=public_id,
            is_active=True,
            created_at=now,
        )
        self.db.add(api_key)
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "api_key.created",
            "api_key",
            now,
            resource_id=api_key.public_id,
            project_id=project.public_id,
            metadata={
                "scopes": requested_scopes,
                "expires_at": api_key.expires_at.isoformat(),
            },
        )
        await self.db.commit()
        await self.db.refresh(api_key)
        return api_key, plaintext

    async def list_api_keys(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[ApiKey]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.KEY_MANAGE
        )
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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.KEY_MANAGE
        )
        api_key = await self.db.scalar(
            select(ApiKey)
            .where(
                ApiKey.project_id == project.id,
                ApiKey.public_id == key_id,
            )
            .with_for_update()
        )
        if api_key is None:
            raise NotFoundError("API key", key_id)
        now = await database_now(self.db)
        api_key.is_active = False
        api_key.revoked_at = api_key.revoked_at or now
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "api_key.revoked",
            "api_key",
            now,
            resource_id=api_key.public_id,
            project_id=project.public_id,
        )
        await self.db.commit()
        await self.db.refresh(api_key)
        return api_key

    async def rotate_api_key(
        self,
        user_id: int,
        project_id: str,
        key_id: str,
        *,
        overlap_seconds: int | None,
    ) -> tuple[ApiKey, str]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.KEY_MANAGE
        )
        previous = await self.db.scalar(
            select(ApiKey)
            .where(
                ApiKey.project_id == project.id,
                ApiKey.public_id == key_id,
            )
            .with_for_update()
        )
        if previous is None:
            raise NotFoundError("API key", key_id)
        replacement_exists = await self.db.scalar(
            select(ApiKey.id).where(ApiKey.rotated_from_id == previous.id)
        )
        if replacement_exists is not None:
            raise ConflictError("API key has already been rotated")
        now = await database_now(self.db)
        if (
            not previous.is_active
            or previous.revoked_at is not None
            or (
                previous.expires_at is not None
                and _aware(previous.expires_at) <= now
            )
        ):
            raise ConflictError("Only an active API key can be rotated")
        scopes = self._validate_scopes(list(previous.scopes))
        plaintext, prefix = await self._generate_unique_api_key()
        replacement = ApiKey(
            public_id=str(uuid4()),
            project_id=project.id,
            name=str(previous.name),
            key_prefix=prefix,
            key_digest=digest_api_key(plaintext, self.settings.api_key_pepper),
            scopes=scopes,
            expires_at=self._api_key_expiry(now, None),
            rotation_family_id=previous.rotation_family_id,
            rotated_from_id=previous.id,
            is_active=True,
            created_at=now,
        )
        overlap = (
            self.settings.api_key_rotation_overlap_seconds
            if overlap_seconds is None
            else overlap_seconds
        )
        if not 1 <= overlap <= 86_400:
            raise ValidationError(
                "API key rotation overlap is invalid", "overlap_seconds"
            )
        overlap_until = now + timedelta(seconds=overlap)
        if (
            previous.expires_at is None
            or _aware(previous.expires_at) > overlap_until
        ):
            previous.expires_at = overlap_until
        self.db.add(replacement)
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "api_key.rotated",
            "api_key",
            now,
            resource_id=replacement.public_id,
            project_id=project.public_id,
            metadata={
                "previous_id": previous.public_id,
                "replacement_id": replacement.public_id,
                "overlap_seconds": overlap,
                "scopes": scopes,
            },
        )
        await self.db.commit()
        await self.db.refresh(replacement)
        return replacement, plaintext

    async def _validate_endpoint_url(self, url: str) -> None:
        try:
            await validate_webhook_url(
                url, bool(self.settings.allow_http_webhooks)
            )
        except UnsafeWebhookUrl as exc:
            record_security_deny(SecurityLayer.ADMISSION, exc.reason)
            raise ValidationError(str(exc), "url") from exc

    async def _endpoint(
        self, project: Project, endpoint_id: str, *, lock: bool = False
    ) -> WebhookEndpoint:
        statement = select(WebhookEndpoint).where(
            WebhookEndpoint.project_id == project.id,
            WebhookEndpoint.public_id == endpoint_id,
        )
        if lock:
            statement = statement.with_for_update()
        endpoint = await self.db.scalar(statement)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint", endpoint_id)
        return endpoint

    async def create_endpoint(
        self, user_id: int, project_id: str, url: str, description: str | None
    ) -> tuple[WebhookEndpoint, str]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_MANAGE
        )
        await self._validate_endpoint_url(url)
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
        self.db.add_all(
            (
                EndpointQuotaState(
                    **endpoint_quota_values(endpoint.id, self.settings, now)
                ),
                EndpointSigningSecretVersion(
                    endpoint_id=endpoint.id,
                    version=1,
                    activated_at=now,
                ),
            )
        )
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "endpoint.created",
            "endpoint",
            now,
            resource_id=endpoint.public_id,
            project_id=project.public_id,
            metadata={"version": 1},
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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
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
        changes: dict[str, object],
    ) -> WebhookEndpoint:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_MANAGE
        )
        endpoint = await self._endpoint(project, endpoint_id, lock=True)
        changed_url = changes.get("url")
        if changed_url is not None:
            await self._validate_endpoint_url(str(changed_url))
        if changes.get("is_active") is True and not endpoint.is_active:
            await AdmissionController(
                self.db, self.settings
            ).check_endpoint_limit(
                project.organization_id,
                project.id,
                exclude_endpoint_id=endpoint.id,
            )
        now = await database_now(self.db)
        for field, value in changes.items():
            setattr(endpoint, field, value)
        endpoint.updated_at = now
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "endpoint.updated",
            "endpoint",
            now,
            resource_id=endpoint.public_id,
            project_id=project.public_id,
            metadata={"changed_fields": sorted(changes)},
        )
        await self.db.commit()
        await self.db.refresh(endpoint)
        return endpoint

    async def deactivate_endpoint(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> WebhookEndpoint:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_MANAGE
        )
        endpoint = await self._endpoint(project, endpoint_id, lock=True)
        now = await database_now(self.db)
        if endpoint.is_active:
            endpoint.is_active = False
            endpoint.updated_at = now
            organization_public_id = await self._project_organization_id(
                project
            )
            await self._audit(
                user_id,
                organization_public_id,
                "endpoint.deactivated",
                "endpoint",
                now,
                resource_id=endpoint.public_id,
                project_id=project.public_id,
            )
        await self.db.commit()
        await self.db.refresh(endpoint)
        return endpoint

    async def rotate_endpoint_secret(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> tuple[WebhookEndpoint, str, datetime | None]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_MANAGE
        )
        endpoint = await self._endpoint(project, endpoint_id, lock=True)
        now = await database_now(self.db)
        current = await self.db.scalar(
            select(EndpointSigningSecretVersion)
            .where(
                EndpointSigningSecretVersion.endpoint_id == endpoint.id,
                EndpointSigningSecretVersion.version
                == endpoint.secret_version,
            )
            .with_for_update()
        )
        if current is None:
            raise ConflictError("Current endpoint signing version is missing")
        current.retire_at = now + timedelta(
            seconds=self.settings.endpoint_secret_overlap_seconds
        )
        previous_version = int(endpoint.secret_version)
        endpoint.secret_version = previous_version + 1
        endpoint.updated_at = now
        self.db.add(
            EndpointSigningSecretVersion(
                endpoint_id=endpoint.id,
                version=endpoint.secret_version,
                activated_at=now,
            )
        )
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "endpoint.signing_version_rotated",
            "endpoint",
            now,
            resource_id=endpoint.public_id,
            project_id=project.public_id,
            metadata={
                "previous_version": previous_version,
                "current_version": endpoint.secret_version,
                "overlap_seconds": (
                    self.settings.endpoint_secret_overlap_seconds
                ),
            },
        )
        previous_valid_until = current.retire_at
        await self.db.commit()
        await self.db.refresh(endpoint)
        return (
            endpoint,
            endpoint_secret(
                self.settings.webhook_signing_key,
                endpoint.public_id,
                endpoint.secret_version,
            ),
            previous_valid_until,
        )

    async def ingest_event(
        self,
        project: Project,
        idempotency_key: str,
        event_type: str,
        payload: object,
        envelope_mode: str = "native",
    ) -> Event:
        project_id = project.id
        key_max_length = self.settings.idempotency_key_max_length
        if not 1 <= len(idempotency_key) <= key_max_length:
            raise ValidationError("Idempotency-Key length is invalid")
        if envelope_mode not in {"native", "cloudevents"}:
            raise ValidationError("Envelope mode is invalid")
        try:
            payload_bytes = canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Payload must be valid finite JSON") from exc
        if len(payload_bytes) > self.settings.webhook_payload_max_bytes:
            raise ValidationError("Webhook payload exceeds configured limit")
        fingerprint_material = event_type.encode("utf-8") + b"\0" + payload_bytes
        if envelope_mode != "native":
            fingerprint_material = (
                envelope_mode.encode("ascii") + b"\0" + fingerprint_material
            )
        fingerprint = hashlib.sha256(fingerprint_material).hexdigest()
        existing = await self.db.scalar(
            select(Event).where(
                Event.project_id == project_id,
                Event.idempotency_key == idempotency_key,
            )
        )
        if existing:
            content_changed = (
                existing.event_type != event_type
                or existing.envelope_mode != envelope_mode
                or existing.payload_hash != fingerprint
            )
            if content_changed:
                raise ConflictError(
                    "Idempotency-Key was already used with different content"
                )
            record_event("idempotent")
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
                or existing.envelope_mode != envelope_mode
                or existing.payload_hash != fingerprint
            )
            if content_changed:
                raise ConflictError(
                    "Idempotency-Key was already used with different content"
                )
            record_event("idempotent")
            return existing
        event_public_id = str(uuid4())
        if envelope_mode == "cloudevents":
            envelope = {
                "data": payload,
                "datacontenttype": "application/json",
                "id": event_public_id,
                "source": (
                    f"urn:webhook-platform:project:{project.public_id}"
                ),
                "specversion": "1.0",
                "time": now.isoformat(),
                "type": event_type,
            }
        else:
            envelope = {
                "id": event_public_id,
                "type": event_type,
                "created_at": now.isoformat(),
                "data": payload,
            }
        canonical_envelope = canonical_json(envelope)
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
        traceparent, tracestate = current_trace_headers()
        event = Event(
            public_id=event_public_id,
            project_id=project_id,
            idempotency_key=idempotency_key,
            event_type=event_type,
            envelope_mode=envelope_mode,
            payload=payload,
            payload_hash=fingerprint,
            canonical_envelope=canonical_envelope,
            traceparent=traceparent,
            tracestate=tracestate,
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
                and raced.envelope_mode == envelope_mode
                and raced.payload_hash == fingerprint
            )
            if raced_matches:
                record_event("idempotent")
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
                    signing_secret_version_snapshot=(endpoint.secret_version),
                    status="pending",
                    attempt_count=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        await self.db.commit()
        await self.db.refresh(event)
        record_event("accepted")
        record_enqueue(len(endpoints))
        return event

    async def list_events(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[Event]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
        event = await self.db.scalar(
            select(Event).where(
                Event.project_id == project.id,
                Event.public_id == event_id,
            )
        )
        if event is None:
            raise NotFoundError("Event", event_id)
        return event

    async def list_deliveries(
        self, user_id: int, project_id: str, offset: int, limit: int
    ) -> list[Delivery]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
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

    async def _replay_source(
        self,
        project: Project,
        delivery_id: str,
        *,
        dead_only: bool,
        lock: bool,
    ) -> Delivery:
        statement = (
            select(Delivery)
            .join(Event)
            .where(
                Event.project_id == project.id,
                Delivery.public_id == delivery_id,
            )
        )
        if dead_only:
            statement = statement.where(Delivery.status == "dead")
        if lock:
            statement = statement.with_for_update(of=Delivery)
        delivery = await self.db.scalar(statement)
        if delivery is None:
            raise NotFoundError("Delivery", delivery_id)
        return delivery

    async def _ensure_replayable(
        self, delivery: Delivery, now: datetime
    ) -> None:
        event = await self.db.get(Event, delivery.event_id)
        if (
            event is None
            or event.payload is None
            or event.canonical_envelope is None
            or event.payload_purged_at is not None
        ):
            raise ConflictError("Delivery event payload has been purged")
        version = await self.db.scalar(
            select(EndpointSigningSecretVersion).where(
                EndpointSigningSecretVersion.endpoint_id
                == delivery.endpoint_id,
                EndpointSigningSecretVersion.version
                == delivery.signing_secret_version_snapshot,
            )
        )
        if version is None:
            raise ConflictError("Delivery signing version is unavailable")
        if version.retire_at is not None and _aware(version.retire_at) <= now:
            raise ConflictError("Delivery signing version has retired")

    @staticmethod
    def _replay_copy(original: Delivery, now: datetime) -> Delivery:
        return Delivery(
            public_id=str(uuid4()),
            organization_id=original.organization_id,
            event_id=original.event_id,
            endpoint_id=original.endpoint_id,
            replay_of_delivery_id=original.id,
            endpoint_public_id_snapshot=(original.endpoint_public_id_snapshot),
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

    async def replay_delivery(
        self, user_id: int, project_id: str, delivery_id: str
    ) -> Delivery:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.REPLAY
        )
        original = await self._replay_source(
            project, delivery_id, dead_only=False, lock=True
        )
        checked_at = await database_now(self.db)
        await self._ensure_replayable(original, checked_at)
        now = await AdmissionController(self.db, self.settings).admit_replay(
            original.organization_id
        )
        replay = self._replay_copy(original, now)
        operation = ReplayOperation(
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
        self.db.add_all((replay, operation))
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "delivery.replayed",
            "replay_operation",
            now,
            resource_id=operation.public_id,
            project_id=project.public_id,
            metadata={"requested_count": 1, "created_count": 1},
        )
        await self.db.commit()
        await self.db.refresh(replay)
        return replay

    async def replay_deliveries(
        self,
        user_id: int,
        project_id: str,
        delivery_ids: list[str],
        idempotency_key: str,
    ) -> ReplayOperation:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.REPLAY
        )
        if not (
            1
            <= len(idempotency_key)
            <= self.settings.idempotency_key_max_length
        ):
            raise ValidationError("Idempotency-Key length is invalid")
        if not (
            1 <= len(delivery_ids) <= self.settings.bulk_replay_max_deliveries
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
        for delivery in sources:
            await self._ensure_replayable(delivery, now)
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
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "delivery.replayed",
            "replay_operation",
            now,
            resource_id=operation.public_id,
            project_id=project.public_id,
            metadata={
                "requested_count": len(delivery_ids),
                "created_count": len(replays),
            },
        )
        await self.db.commit()
        await self.db.refresh(operation)
        return operation

    async def _dead_deliveries(
        self,
        project: Project,
        offset: int,
        limit: int,
        endpoint_id: str | None,
        reason: str | None,
        minimum_age_seconds: int | None,
        now: datetime | None,
    ) -> list[Delivery]:
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
            if now is None:
                raise RuntimeError("Database time is required for age filters")
            query = query.where(
                Delivery.dead_at
                <= now - timedelta(seconds=minimum_age_seconds)
            )
        result = await self.db.scalars(
            query.order_by(Delivery.dead_at.desc(), Delivery.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
        now = (
            await database_now(self.db)
            if minimum_age_seconds is not None
            else None
        )
        return await self._dead_deliveries(
            project,
            offset,
            limit,
            endpoint_id,
            reason,
            minimum_age_seconds,
            now,
        )

    async def export_dead_deliveries(
        self,
        user_id: int,
        project_id: str,
        *,
        endpoint_id: str | None,
        reason: str | None,
        minimum_age_seconds: int | None,
        limit: int,
    ) -> list[Delivery]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.PROJECT_READ
        )
        bounded_limit = min(limit, 1_000)
        now = (
            await database_now(self.db)
            if minimum_age_seconds is not None
            else None
        )
        return await self._dead_deliveries(
            project,
            0,
            bounded_limit,
            endpoint_id,
            reason,
            minimum_age_seconds,
            now,
        )

    async def _endpoint_runtime(
        self, project: Project, endpoint_id: str, *, lock: bool
    ) -> tuple[WebhookEndpoint, EndpointQuotaState]:
        endpoint = await self._endpoint(project, endpoint_id, lock=lock)
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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_OPERATE
        )
        endpoint, state = await self._endpoint_runtime(
            project, endpoint_id, lock=True
        )
        now = await database_now(self.db)
        state.paused_at = state.paused_at or now
        state.pause_reason = reason
        state.updated_at = now
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "endpoint.paused",
            "endpoint",
            now,
            resource_id=endpoint.public_id,
            project_id=project.public_id,
            metadata={"reason_provided": reason is not None},
        )
        await self.db.commit()
        return self._runtime_view(endpoint, state)

    async def resume_endpoint(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> dict[str, object]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_OPERATE
        )
        endpoint, state = await self._endpoint_runtime(
            project, endpoint_id, lock=True
        )
        now = await database_now(self.db)
        state.paused_at = None
        state.pause_reason = None
        state.updated_at = now
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "endpoint.resumed",
            "endpoint",
            now,
            resource_id=endpoint.public_id,
            project_id=project.public_id,
        )
        await self.db.commit()
        return self._runtime_view(endpoint, state)

    async def recover_endpoint_circuit(
        self, user_id: int, project_id: str, endpoint_id: str
    ) -> dict[str, object]:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.ENDPOINT_OPERATE
        )
        endpoint, state = await self._endpoint_runtime(
            project, endpoint_id, lock=True
        )
        now = await database_now(self.db)
        state.retry_tokens = float(self.settings.endpoint_retry_burst)
        state.retry_refilled_at = now
        state.circuit_state = "closed"
        state.consecutive_failures = 0
        state.circuit_open_until = None
        state.half_open_probe_delivery_id = None
        state.updated_at = now
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "endpoint.circuit_recovered",
            "endpoint",
            now,
            resource_id=endpoint.public_id,
            project_id=project.public_id,
        )
        await self.db.commit()
        return self._runtime_view(endpoint, state)

    async def cancel_delivery(
        self,
        user_id: int,
        project_id: str,
        delivery_id: str,
        reason: str | None,
    ) -> Delivery:
        project = await authorize_project(
            self.db, user_id, project_id, Permission.DELIVERY_CANCEL
        )
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
        now = await database_now(self.db)
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
        organization_public_id = await self._project_organization_id(project)
        await self._audit(
            user_id,
            organization_public_id,
            "delivery.canceled",
            "delivery",
            now,
            resource_id=delivery.public_id,
            project_id=project.public_id,
            metadata={"reason_provided": reason is not None},
        )
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
        project = await authorize_project(
            self.db, user_id, project_id, Permission.DELIVERY_PURGE
        )
        now = await database_now(self.db)
        limit = min(max_records, self.settings.delivery_purge_batch_size)
        cutoff = now - timedelta(days=self.settings.delivery_retention_days)
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
        delivery_ids = list(await self.db.scalars(eligible))
        if not dry_run:
            if delivery_ids:
                await self.db.execute(
                    delete(Delivery).where(Delivery.id.in_(delivery_ids))
                )
            organization_public_id = await self._project_organization_id(
                project
            )
            await self._audit(
                user_id,
                organization_public_id,
                "delivery.purged",
                "delivery",
                now,
                project_id=project.public_id,
                metadata={
                    "matched_count": len(delivery_ids),
                    "purged_count": len(delivery_ids),
                    "retention_days": (self.settings.delivery_retention_days),
                },
            )
            await self.db.commit()
        return {
            "cutoff": cutoff,
            "matched": len(delivery_ids),
            "purged": 0 if dry_run else len(delivery_ids),
            "dry_run": dry_run,
        }
