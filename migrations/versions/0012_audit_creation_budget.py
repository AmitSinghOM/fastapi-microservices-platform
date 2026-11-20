"""Index durable per-user organization creation budgets."""

from alembic import op

revision = "0012_audit_creation_budget"
down_revision = "0011_signature_scheme"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_administrative_audit_events_actor_action",
        "administrative_audit_events",
        ["actor_user_id", "action"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_administrative_audit_events_actor_action",
        table_name="administrative_audit_events",
    )
