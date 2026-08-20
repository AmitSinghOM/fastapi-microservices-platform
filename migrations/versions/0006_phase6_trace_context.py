"""Persist bounded W3C trace context across the durable queue."""

import sqlalchemy as sa
from alembic import op

revision = "0006_phase6_trace_context"
down_revision = "0005_phase5_retry_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("traceparent", sa.String(55)))
    op.add_column("events", sa.Column("tracestate", sa.String(512)))


def downgrade() -> None:
    op.drop_column("events", "tracestate")
    op.drop_column("events", "traceparent")
