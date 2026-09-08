"""Shared per-account login throttling state.

The process-local rate-limit middleware multiplies by replica count and
resets on restart. Credential brute-force protection therefore moves to a
database table shared by every API replica.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_login_throttle"
down_revision = "0008_phase8_envelope_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_throttles",
        sa.Column("scope", sa.String(340), primary_key=True),
        sa.Column(
            "failure_count", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "window_started_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("login_throttles")
