"""Tenant-safe organization and project authorization primitives."""

from enum import Enum
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ForbiddenError, NotFoundError
from app.models import (
    Organization,
    OrganizationMember,
    Project,
    ProjectMember,
    User,
)


class Permission(str, Enum):
    ORG_READ = "org:read"
    MEMBER_MANAGE = "member:manage"
    POLICY_MANAGE = "policy:manage"
    ORG_EXPORT = "org:export"
    ORG_DELETE = "org:delete"
    AUDIT_READ = "audit:read"
    PROJECT_READ = "project:read"
    PROJECT_MANAGE = "project:manage"
    KEY_MANAGE = "key:manage"
    ENDPOINT_MANAGE = "endpoint:manage"
    ENDPOINT_OPERATE = "endpoint:operate"
    REPLAY = "replay"
    DELIVERY_CANCEL = "delivery:cancel"
    DELIVERY_PURGE = "delivery:purge"


class OrganizationRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class ProjectRole(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


_ORGANIZATION_ADMIN_PERMISSIONS = frozenset(Permission) - {
    Permission.ORG_DELETE,
}
ORGANIZATION_ROLE_PERMISSIONS: dict[
    OrganizationRole, frozenset[Permission]
] = {
    OrganizationRole.OWNER: frozenset(Permission),
    OrganizationRole.ADMIN: _ORGANIZATION_ADMIN_PERMISSIONS,
    OrganizationRole.MEMBER: frozenset({Permission.ORG_READ}),
}

_PROJECT_ADMIN_PERMISSIONS = frozenset(
    {
        Permission.PROJECT_READ,
        Permission.PROJECT_MANAGE,
        Permission.KEY_MANAGE,
        Permission.ENDPOINT_MANAGE,
        Permission.ENDPOINT_OPERATE,
        Permission.REPLAY,
        Permission.DELIVERY_CANCEL,
        Permission.DELIVERY_PURGE,
    }
)
PROJECT_ROLE_PERMISSIONS: dict[ProjectRole, frozenset[Permission]] = {
    ProjectRole.ADMIN: _PROJECT_ADMIN_PERMISSIONS,
    ProjectRole.OPERATOR: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.ENDPOINT_OPERATE,
            Permission.REPLAY,
            Permission.DELIVERY_CANCEL,
        }
    ),
    ProjectRole.VIEWER: frozenset({Permission.PROJECT_READ}),
}

_ORGANIZATION_PERMISSIONS = frozenset(
    {
        Permission.ORG_READ,
        Permission.MEMBER_MANAGE,
        Permission.POLICY_MANAGE,
        Permission.ORG_EXPORT,
        Permission.ORG_DELETE,
        Permission.AUDIT_READ,
    }
)
_PROJECT_PERMISSIONS = frozenset(Permission) - _ORGANIZATION_PERMISSIONS
_PENDING_READ_PERMISSIONS = frozenset(
    {
        Permission.ORG_READ,
        Permission.ORG_EXPORT,
        Permission.AUDIT_READ,
        Permission.PROJECT_READ,
    }
)


def _user_id(user: User | int) -> int:
    return cast(int, user.id) if isinstance(user, User) else user


def _ensure_lifecycle_allows(
    organization: Organization,
    permission: Permission,
    allow_deletion_pending: bool,
) -> None:
    if organization.lifecycle_state == "active":
        return
    if permission in _PENDING_READ_PERMISSIONS:
        return
    if allow_deletion_pending and permission is Permission.ORG_DELETE:
        return
    raise ForbiddenError(
        "Organization mutations are disabled while deletion is pending"
    )


async def authorize_organization(
    db: AsyncSession,
    user: User | int,
    organization_public_id: str,
    permission: Permission,
    *,
    allow_deletion_pending: bool = False,
) -> Organization:
    """Return a visible organization after enforcing role and lifecycle."""
    row = (
        await db.execute(
            select(Organization, OrganizationMember.role)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                Organization.public_id == organization_public_id,
                OrganizationMember.user_id == _user_id(user),
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Organization", organization_public_id)

    organization, role_value = row
    role = OrganizationRole(role_value)
    if permission not in ORGANIZATION_ROLE_PERMISSIONS[role]:
        raise ForbiddenError()
    _ensure_lifecycle_allows(organization, permission, allow_deletion_pending)
    return organization


async def authorize_project(
    db: AsyncSession,
    user: User | int,
    project_public_id: str,
    permission: Permission,
    *,
    allow_deletion_pending: bool = False,
) -> Project:
    """Return a visible project after enforcing tenant and project roles."""
    row = (
        await db.execute(
            select(Project, Organization, OrganizationMember.role)
            .join(
                Organization,
                Organization.id == Project.organization_id,
            )
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                Project.public_id == project_public_id,
                OrganizationMember.user_id == _user_id(user),
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Project", project_public_id)

    project, organization, organization_role_value = row
    organization_role = OrganizationRole(organization_role_value)
    if organization_role in {
        OrganizationRole.OWNER,
        OrganizationRole.ADMIN,
    }:
        if permission not in _PROJECT_PERMISSIONS:
            raise ForbiddenError()
        _ensure_lifecycle_allows(
            organization, permission, allow_deletion_pending
        )
        return project

    project_role_value = await db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == _user_id(user),
        )
    )
    if project_role_value is None:
        raise NotFoundError("Project", project_public_id)

    project_role = ProjectRole(project_role_value)
    if permission not in PROJECT_ROLE_PERMISSIONS[project_role]:
        raise ForbiddenError()
    _ensure_lifecycle_allows(organization, permission, allow_deletion_pending)
    return project
