"""Add optional CloudEvents-compatible event envelopes."""

import sqlalchemy as sa
from alembic import op

revision = "0008_phase8_envelope_mode"
down_revision = "0007_phase7_tenant_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "envelope_mode",
                sa.String(16),
                nullable=False,
                server_default="native",
            )
        )
        batch_op.create_check_constraint(
            "ck_events_envelope_mode",
            "envelope_mode IN ('native', 'cloudevents')",
        )


def downgrade() -> None:
    events = sa.table(
        "events", sa.column("envelope_mode", sa.String(16))
    )
    cloud_event_count = op.get_bind().scalar(
        sa.select(sa.func.count())
        .select_from(events)
        .where(events.c.envelope_mode == "cloudevents")
    )
    if cloud_event_count:
        raise RuntimeError(
            "Cannot downgrade Phase 8 while CloudEvents envelopes exist"
        )
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_constraint(
            "ck_events_envelope_mode", type_="check"
        )
        batch_op.drop_column("envelope_mode")
