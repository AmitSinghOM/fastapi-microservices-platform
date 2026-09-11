"""Add Phase 7 tenant lifecycle and authorization foundations."""

import sqlalchemy as sa
from alembic import op

revision = "0007_phase7_tenant_lifecycle"
down_revision = "0006_phase6_trace_context"
branch_labels = None
depends_on = None


_ACTIVE_OPERATION_PREDICATE = "status IN ('pending', 'running')"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_audit_immutability_trigger() -> None:
    if not _is_postgresql():
        return
    op.execute(
        """
        CREATE FUNCTION reject_administrative_audit_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'administrative audit events are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_administrative_audit_events_immutable
        BEFORE UPDATE OR DELETE ON administrative_audit_events
        FOR EACH ROW
        EXECUTE FUNCTION reject_administrative_audit_event_mutation()
        """
    )


def _drop_audit_immutability_trigger() -> None:
    if not _is_postgresql():
        return
    op.execute(
        "DROP TRIGGER IF EXISTS trg_administrative_audit_events_immutable "
        "ON administrative_audit_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS reject_administrative_audit_event_mutation()"
    )


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_state",
                sa.String(24),
                nullable=False,
                server_default="active",
            )
        )
        batch_op.add_column(
            sa.Column("deletion_requested_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(
            sa.Column("deletion_scheduled_at", sa.DateTime(timezone=True))
        )
        batch_op.create_check_constraint(
            "ck_organizations_lifecycle_state",
            "lifecycle_state IN ('active', 'deletion_pending')",
        )

    with op.batch_alter_table("organization_members") as batch_op:
        batch_op.drop_constraint("ck_org_members_role", type_="check")
    members = sa.table(
        "organization_members",
        sa.column("role", sa.String()),
    )
    op.get_bind().execute(
        members.update().where(members.c.role == "member").values(role="admin")
    )
    with op.batch_alter_table("organization_members") as batch_op:
        batch_op.create_check_constraint(
            "ck_org_members_role",
            "role IN ('owner', 'admin', 'member')",
        )

    op.create_table(
        "organization_policies",
        sa.Column("organization_id", sa.Integer(), primary_key=True),
        sa.Column(
            "plan", sa.String(16), nullable=False, server_default="free"
        ),
        sa.Column(
            "payload_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "response_retention_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "plan IN ('free', 'standard', 'enterprise')",
            name="ck_organization_policies_plan",
        ),
        sa.CheckConstraint(
            "payload_retention_days > 0",
            name="ck_organization_policies_payload_retention",
        ),
        sa.CheckConstraint(
            "response_retention_days > 0",
            name="ck_organization_policies_response_retention",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
    )
    organizations = sa.table(
        "organizations",
        sa.column("id", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    policies = sa.table(
        "organization_policies",
        sa.column("organization_id"),
        sa.column("plan"),
        sa.column("payload_retention_days"),
        sa.column("response_retention_days"),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    op.get_bind().execute(
        sa.insert(policies).from_select(
            list(policies.c.keys()),
            sa.select(
                organizations.c.id,
                sa.literal("free"),
                sa.literal(30),
                sa.literal(30),
                organizations.c.created_at,
                organizations.c.created_at,
            ),
        )
    )

    op.create_table(
        "project_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('admin', 'operator', 'viewer')",
            name="ck_project_members_role",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "project_id", "user_id", name="uq_project_members_project_user"
        ),
    )
    op.create_index(
        "ix_project_members_user_project",
        "project_members",
        ["user_id", "project_id"],
    )

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(
            sa.Column(
                "scopes",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[\"events:write\"]'"),
            )
        )
        batch_op.add_column(
            sa.Column("expires_at", sa.DateTime(timezone=True))
        )
        batch_op.add_column(sa.Column("rotation_family_id", sa.String(36)))
        batch_op.add_column(sa.Column("rotated_from_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_api_keys_rotated_from",
            "api_keys",
            ["rotated_from_id"],
            ["id"],
            ondelete="SET NULL",
        )
    api_keys = sa.table(
        "api_keys",
        sa.column("public_id", sa.String()),
        sa.column("scopes", sa.JSON()),
        sa.column("rotation_family_id", sa.String()),
    )
    op.get_bind().execute(
        api_keys.update().values(
            scopes=["events:write"],
            rotation_family_id=api_keys.c.public_id,
        )
    )
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column(
            "rotation_family_id",
            existing_type=sa.String(36),
            nullable=False,
        )
    op.create_index(
        "ix_api_keys_expiry_active",
        "api_keys",
        ["expires_at", "is_active"],
    )

    op.create_table(
        "endpoint_signing_secret_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retire_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "version >= 1", name="ck_endpoint_signing_secrets_version"
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["webhook_endpoints.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "endpoint_id",
            "version",
            name="uq_endpoint_signing_secrets_endpoint_version",
        ),
    )
    op.create_index(
        "ix_endpoint_signing_secrets_retirement",
        "endpoint_signing_secret_versions",
        ["endpoint_id", "retire_at"],
    )
    endpoints = sa.table(
        "webhook_endpoints",
        sa.column("id", sa.Integer()),
        sa.column("secret_version", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    secret_versions = sa.table(
        "endpoint_signing_secret_versions",
        sa.column("endpoint_id"),
        sa.column("version"),
        sa.column("activated_at"),
        sa.column("retire_at"),
    )
    op.get_bind().execute(
        sa.insert(secret_versions).from_select(
            list(secret_versions.c.keys()),
            sa.select(
                endpoints.c.id,
                endpoints.c.secret_version,
                endpoints.c.created_at,
                sa.null(),
            ),
        )
    )

    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(
            sa.Column("payload_purged_at", sa.DateTime(timezone=True))
        )
        batch_op.alter_column(
            "payload", existing_type=sa.JSON(), nullable=True
        )
        batch_op.alter_column(
            "canonical_envelope",
            existing_type=sa.LargeBinary(),
            nullable=True,
        )
    with op.batch_alter_table("delivery_attempts") as batch_op:
        batch_op.add_column(
            sa.Column("response_purged_at", sa.DateTime(timezone=True))
        )

    op.create_table(
        "administrative_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("organization_public_id", sa.String(36), nullable=False),
        sa.Column("project_public_id", sa.String(36)),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_public_id", sa.String(36)),
        sa.Column("sanitized_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_administrative_audit_events_public_id",
        "administrative_audit_events",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_administrative_audit_events_org_created",
        "administrative_audit_events",
        ["organization_public_id", "created_at", "id"],
    )
    _create_audit_immutability_trigger()

    op.create_table(
        "organization_lifecycle_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer()),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(500)),
        sa.CheckConstraint(
            "kind = 'deletion'", name="ck_org_lifecycle_operations_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'canceled', "
            "'failed')",
            name="ck_org_lifecycle_operations_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_organization_lifecycle_operations_public_id",
        "organization_lifecycle_operations",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_org_lifecycle_operations_org_created",
        "organization_lifecycle_operations",
        ["organization_id", "created_at", "id"],
    )
    op.create_index(
        "ix_org_lifecycle_operations_status_scheduled",
        "organization_lifecycle_operations",
        ["status", "scheduled_at", "id"],
    )
    op.create_index(
        "uq_org_lifecycle_operations_active",
        "organization_lifecycle_operations",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_OPERATION_PREDICATE),
        sqlite_where=sa.text(_ACTIVE_OPERATION_PREDICATE),
    )


def downgrade() -> None:
    connection = op.get_bind()
    events = sa.table(
        "events",
        sa.column("payload", sa.JSON()),
        sa.column("canonical_envelope", sa.LargeBinary()),
    )
    purged_event_count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(events)
        .where(
            sa.or_(
                events.c.payload.is_(None),
                events.c.canonical_envelope.is_(None),
            )
        )
    )
    if purged_event_count:
        raise RuntimeError(
            "Cannot downgrade Phase 7 after event payloads have been purged"
        )

    organizations = sa.table(
        "organizations", sa.column("lifecycle_state", sa.String())
    )
    pending_organization_count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(organizations)
        .where(organizations.c.lifecycle_state != "active")
    )
    if pending_organization_count:
        raise RuntimeError(
            "Cannot downgrade Phase 7 while organization deletion is pending"
        )

    op.drop_table("organization_lifecycle_operations")
    _drop_audit_immutability_trigger()
    op.drop_table("administrative_audit_events")

    with op.batch_alter_table("delivery_attempts") as batch_op:
        batch_op.drop_column("response_purged_at")
    with op.batch_alter_table("events") as batch_op:
        batch_op.alter_column(
            "canonical_envelope",
            existing_type=sa.LargeBinary(),
            nullable=False,
        )
        batch_op.alter_column(
            "payload", existing_type=sa.JSON(), nullable=False
        )
        batch_op.drop_column("payload_purged_at")

    op.drop_table("endpoint_signing_secret_versions")
    op.drop_index("ix_api_keys_expiry_active", table_name="api_keys")
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_constraint(
            "fk_api_keys_rotated_from", type_="foreignkey"
        )
        batch_op.drop_column("rotated_from_id")
        batch_op.drop_column("rotation_family_id")
        batch_op.drop_column("expires_at")
        batch_op.drop_column("scopes")

    op.drop_table("project_members")
    op.drop_table("organization_policies")

    members = sa.table("organization_members", sa.column("role", sa.String()))
    connection.execute(
        members.update().where(members.c.role == "admin").values(role="member")
    )
    with op.batch_alter_table("organization_members") as batch_op:
        batch_op.drop_constraint("ck_org_members_role", type_="check")
        batch_op.create_check_constraint(
            "ck_org_members_role", "role IN ('owner', 'member')"
        )

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_constraint(
            "ck_organizations_lifecycle_state", type_="check"
        )
        batch_op.drop_column("deletion_scheduled_at")
        batch_op.drop_column("deletion_requested_at")
        batch_op.drop_column("lifecycle_state")
