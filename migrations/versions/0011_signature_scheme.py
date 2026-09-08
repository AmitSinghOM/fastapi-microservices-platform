"""Per-endpoint wire signature scheme with delivery snapshots (ADR 0002).

``legacy`` preserves today's exact signature bytes and stays the default.
``standard`` emits the Standard Webhooks header format. Deliveries capture
the scheme at acceptance; later endpoint edits never re-sign accepted work.

Downgrade refuses while any non-legacy row exists, mirroring the 0008
envelope-mode rule: dropping the column would silently change the wire
format of pending deliveries.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_signature_scheme"
down_revision = "0010_endpoint_event_types"
branch_labels = None
depends_on = None

_SCHEMES = "('legacy', 'standard')"


def upgrade() -> None:
    op.add_column(
        "webhook_endpoints",
        sa.Column(
            "signature_scheme",
            sa.String(16),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.create_check_constraint(
        "ck_webhook_endpoints_signature_scheme",
        "webhook_endpoints",
        f"signature_scheme IN {_SCHEMES}",
    )
    op.add_column(
        "deliveries",
        sa.Column(
            "signature_scheme_snapshot",
            sa.String(16),
            nullable=False,
            server_default="legacy",
        ),
    )
    op.create_check_constraint(
        "ck_deliveries_snapshot_signature_scheme",
        "deliveries",
        f"signature_scheme_snapshot IN {_SCHEMES}",
    )


def downgrade() -> None:
    connection = op.get_bind()
    non_legacy = connection.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM webhook_endpoints"
            " WHERE signature_scheme != 'legacy')"
            " + (SELECT COUNT(*) FROM deliveries"
            " WHERE signature_scheme_snapshot != 'legacy')"
        )
    ).scalar()
    if non_legacy:
        raise RuntimeError(
            "Refusing downgrade: non-legacy signature schemes exist; "
            "revert affected endpoints to 'legacy' and drain non-legacy "
            "deliveries first"
        )
    op.drop_constraint(
        "ck_deliveries_snapshot_signature_scheme",
        "deliveries",
        type_="check",
    )
    op.drop_column("deliveries", "signature_scheme_snapshot")
    op.drop_constraint(
        "ck_webhook_endpoints_signature_scheme",
        "webhook_endpoints",
        type_="check",
    )
    op.drop_column("webhook_endpoints", "signature_scheme")
