"""Add shared admission quotas and tenant-fair delivery ownership."""

import sqlalchemy as sa
from alembic import op

revision = "0004_phase4_admission_fairness"
down_revision = "0003_phase2_delivery_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deliveries", sa.Column("organization_id", sa.Integer())
    )
    connection = op.get_bind()
    deliveries = sa.table(
        "deliveries",
        sa.column("id", sa.Integer()),
        sa.column("event_id", sa.Integer()),
        sa.column("organization_id", sa.Integer()),
    )
    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("project_id", sa.Integer()),
    )
    projects = sa.table(
        "projects",
        sa.column("id", sa.Integer()),
        sa.column("organization_id", sa.Integer()),
    )
    organization_for_event = (
        sa.select(projects.c.organization_id)
        .select_from(
            events.join(projects, events.c.project_id == projects.c.id)
        )
        .where(events.c.id == deliveries.c.event_id)
        .scalar_subquery()
    )
    connection.execute(
        deliveries.update().values(
            organization_id=organization_for_event
        )
    )
    op.alter_column("deliveries", "organization_id", nullable=False)
    op.create_foreign_key(
        "fk_deliveries_organization",
        "deliveries",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_deliveries_fair_due",
        "deliveries",
        [
            "organization_id",
            "endpoint_id",
            "status",
            "next_attempt_at",
            "id",
        ],
    )

    op.create_table(
        "global_control_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_cursor_organization_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_global_control_singleton"),
    )
    op.create_table(
        "tenant_quota_state",
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("event_tokens", sa.Float(), nullable=False),
        sa.Column(
            "event_refilled_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("delivery_tokens", sa.Float(), nullable=False),
        sa.Column(
            "delivery_refilled_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("replay_tokens", sa.Float(), nullable=False),
        sa.Column(
            "replay_refilled_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("endpoint_cursor_id", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "endpoint_quota_state",
        sa.Column(
            "endpoint_id",
            sa.Integer(),
            sa.ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("delivery_tokens", sa.Float(), nullable=False),
        sa.Column("refilled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    now = sa.func.now()
    connection.execute(
        sa.insert(sa.table(
            "global_control_state",
            sa.column("id"),
            sa.column("tenant_cursor_organization_id"),
            sa.column("created_at"),
            sa.column("updated_at"),
        )).values(
            id=1,
            tenant_cursor_organization_id=None,
            created_at=now,
            updated_at=now,
        )
    )
    organizations = sa.table("organizations", sa.column("id", sa.Integer()))
    tenant_state = sa.table(
        "tenant_quota_state",
        sa.column("organization_id"),
        sa.column("event_tokens"),
        sa.column("event_refilled_at"),
        sa.column("delivery_tokens"),
        sa.column("delivery_refilled_at"),
        sa.column("replay_tokens"),
        sa.column("replay_refilled_at"),
        sa.column("endpoint_cursor_id"),
        sa.column("updated_at"),
    )
    connection.execute(
        sa.insert(tenant_state).from_select(
            list(tenant_state.c.keys()),
            sa.select(
                organizations.c.id,
                sa.literal(1_000_000_000.0),
                now,
                sa.literal(1_000_000_000.0),
                now,
                sa.literal(1_000_000_000.0),
                now,
                sa.null(),
                now,
            ),
        )
    )
    endpoints = sa.table(
        "webhook_endpoints", sa.column("id", sa.Integer())
    )
    endpoint_state = sa.table(
        "endpoint_quota_state",
        sa.column("endpoint_id"),
        sa.column("delivery_tokens"),
        sa.column("refilled_at"),
        sa.column("updated_at"),
    )
    connection.execute(
        sa.insert(endpoint_state).from_select(
            list(endpoint_state.c.keys()),
            sa.select(
                endpoints.c.id,
                sa.literal(1_000_000_000.0),
                now,
                now,
            ),
        )
    )


def downgrade() -> None:
    op.drop_table("endpoint_quota_state")
    op.drop_table("tenant_quota_state")
    op.drop_table("global_control_state")
    op.drop_index("ix_deliveries_fair_due", table_name="deliveries")
    op.drop_constraint(
        "fk_deliveries_organization", "deliveries", type_="foreignkey"
    )
    op.drop_column("deliveries", "organization_id")
