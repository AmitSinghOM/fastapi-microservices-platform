"""Per-endpoint event-type subscriptions.

``webhook_endpoints.event_types`` is NULL for existing rows, which keeps
the previous behavior: every active endpoint receives every event. A JSON
list restricts fan-out to matching event types at acceptance time.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_endpoint_event_types"
down_revision = "0009_login_throttle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_endpoints", sa.Column("event_types", sa.JSON, nullable=True)
    )


def downgrade() -> None:
    op.drop_column("webhook_endpoints", "event_types")
