"""Add bounded retries, circuit state, and delivery operations."""

import sqlalchemy as sa
from alembic import op

revision = "0005_phase5_retry_operations"
down_revision = "0004_phase4_admission_fairness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "endpoint_quota_state", sa.Column("retry_tokens", sa.Float())
    )
    op.add_column(
        "endpoint_quota_state",
        sa.Column("retry_refilled_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "endpoint_quota_state", sa.Column("circuit_state", sa.String(16))
    )
    op.add_column(
        "endpoint_quota_state", sa.Column("consecutive_failures", sa.Integer())
    )
    op.add_column(
        "endpoint_quota_state",
        sa.Column("circuit_open_until", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "endpoint_quota_state",
        sa.Column("half_open_probe_delivery_id", sa.Integer()),
    )
    op.add_column(
        "endpoint_quota_state",
        sa.Column("paused_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "endpoint_quota_state", sa.Column("pause_reason", sa.String(200))
    )

    state = sa.table(
        "endpoint_quota_state",
        sa.column("retry_tokens"),
        sa.column("retry_refilled_at"),
        sa.column("circuit_state"),
        sa.column("consecutive_failures"),
        sa.column("updated_at"),
    )
    op.get_bind().execute(
        state.update().values(
            retry_tokens=10.0,
            retry_refilled_at=state.c.updated_at,
            circuit_state="closed",
            consecutive_failures=0,
        )
    )

    for column in (
        "retry_tokens",
        "retry_refilled_at",
        "circuit_state",
        "consecutive_failures",
    ):
        op.alter_column("endpoint_quota_state", column, nullable=False)
    op.create_check_constraint(
        "ck_endpoint_quota_circuit_state",
        "endpoint_quota_state",
        "circuit_state IN ('closed', 'open', 'half_open')",
    )
    op.create_check_constraint(
        "ck_endpoint_quota_failures_nonnegative",
        "endpoint_quota_state",
        "consecutive_failures >= 0",
    )
    op.create_foreign_key(
        "fk_endpoint_quota_half_open_delivery",
        "endpoint_quota_state",
        "deliveries",
        ["half_open_probe_delivery_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint(
        "ck_deliveries_status", "deliveries", type_="check"
    )
    op.create_check_constraint(
        "ck_deliveries_status",
        "deliveries",
        "status IN ('pending', 'processing', 'retry_scheduled', "
        "'succeeded', 'dead', 'canceled')",
    )
    op.add_column(
        "deliveries", sa.Column("dead_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "deliveries", sa.Column("dead_reason", sa.String(64))
    )
    op.add_column(
        "deliveries", sa.Column("canceled_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "deliveries", sa.Column("canceled_reason", sa.String(200))
    )
    deliveries = sa.table(
        "deliveries",
        sa.column("status"),
        sa.column("updated_at"),
        sa.column("dead_at"),
        sa.column("dead_reason"),
    )
    op.get_bind().execute(
        deliveries.update()
        .where(deliveries.c.status == "dead")
        .values(dead_at=deliveries.c.updated_at, dead_reason="legacy_dead")
    )
    op.create_index(
        "ix_deliveries_dead_operations",
        "deliveries",
        ["organization_id", "status", "dead_at", "id"],
    )
    op.create_index(
        "ix_deliveries_endpoint_dead",
        "deliveries",
        ["endpoint_id", "status", "dead_at", "id"],
    )


    op.create_table(
        "replay_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("source_delivery_ids", sa.JSON(), nullable=False),
        sa.Column("created_delivery_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('single', 'bulk')",
            name="ck_replay_operations_mode",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_replay_operations_project_key",
        ),
    )
    op.create_index(
        "ix_replay_operations_public_id",
        "replay_operations",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_replay_operations_org_created",
        "replay_operations",
        ["organization_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_table("replay_operations")
    op.drop_index("ix_deliveries_endpoint_dead", table_name="deliveries")
    op.drop_index("ix_deliveries_dead_operations", table_name="deliveries")
    op.drop_column("deliveries", "canceled_reason")
    op.drop_column("deliveries", "canceled_at")
    op.drop_column("deliveries", "dead_reason")
    op.drop_column("deliveries", "dead_at")
    op.drop_constraint(
        "ck_deliveries_status", "deliveries", type_="check"
    )
    op.create_check_constraint(
        "ck_deliveries_status",
        "deliveries",
        "status IN ('pending', 'processing', 'retry_scheduled', "
        "'succeeded', 'dead')",
    )

    op.drop_constraint(
        "fk_endpoint_quota_half_open_delivery",
        "endpoint_quota_state",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_endpoint_quota_failures_nonnegative",
        "endpoint_quota_state",
        type_="check",
    )
    op.drop_constraint(
        "ck_endpoint_quota_circuit_state",
        "endpoint_quota_state",
        type_="check",
    )
    op.drop_column("endpoint_quota_state", "pause_reason")
    op.drop_column("endpoint_quota_state", "paused_at")
    op.drop_column(
        "endpoint_quota_state", "half_open_probe_delivery_id"
    )
    op.drop_column("endpoint_quota_state", "circuit_open_until")
    op.drop_column("endpoint_quota_state", "consecutive_failures")
    op.drop_column("endpoint_quota_state", "circuit_state")
    op.drop_column("endpoint_quota_state", "retry_refilled_at")
    op.drop_column("endpoint_quota_state", "retry_tokens")