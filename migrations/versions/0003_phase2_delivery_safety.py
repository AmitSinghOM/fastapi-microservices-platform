"""Add immutable event and endpoint delivery snapshots."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0003_phase2_delivery_safety"
down_revision = "0002_drop_duplicate_uniques"
branch_labels = None
depends_on = None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def upgrade() -> None:
    op.add_column(
        "events", sa.Column("canonical_envelope", sa.LargeBinary())
    )
    op.add_column(
        "deliveries",
        sa.Column("endpoint_public_id_snapshot", sa.String(36)),
    )
    op.add_column(
        "deliveries", sa.Column("endpoint_url_snapshot", sa.String(2048))
    )
    op.add_column(
        "deliveries", sa.Column("endpoint_active_snapshot", sa.Boolean())
    )
    op.add_column(
        "deliveries",
        sa.Column("signing_secret_version_snapshot", sa.Integer()),
    )

    connection = op.get_bind()
    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("public_id", sa.String()),
        sa.column("event_type", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("canonical_envelope", sa.LargeBinary()),
    )
    for row in connection.execute(
        sa.select(
            events.c.id,
            events.c.public_id,
            events.c.event_type,
            events.c.payload,
            events.c.created_at,
        )
    ).mappings():
        envelope = _canonical_json(
            {
                "id": row["public_id"],
                "type": row["event_type"],
                "created_at": row["created_at"].isoformat(),
                "data": row["payload"],
            }
        )
        connection.execute(
            events.update()
            .where(events.c.id == row["id"])
            .values(canonical_envelope=envelope)
        )

    deliveries = sa.table(
        "deliveries",
        sa.column("id", sa.Integer()),
        sa.column("endpoint_id", sa.Integer()),
        sa.column("endpoint_public_id_snapshot", sa.String()),
        sa.column("endpoint_url_snapshot", sa.String()),
        sa.column("endpoint_active_snapshot", sa.Boolean()),
        sa.column("signing_secret_version_snapshot", sa.Integer()),
    )
    endpoints = sa.table(
        "webhook_endpoints",
        sa.column("id", sa.Integer()),
        sa.column("public_id", sa.String()),
        sa.column("url", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("secret_version", sa.Integer()),
    )
    snapshot_rows = connection.execute(
        sa.select(
            deliveries.c.id,
            endpoints.c.public_id,
            endpoints.c.url,
            endpoints.c.is_active,
            endpoints.c.secret_version,
        ).join(endpoints, deliveries.c.endpoint_id == endpoints.c.id)
    ).mappings()
    for row in snapshot_rows:
        connection.execute(
            deliveries.update()
            .where(deliveries.c.id == row["id"])
            .values(
                endpoint_public_id_snapshot=row["public_id"],
                endpoint_url_snapshot=row["url"],
                endpoint_active_snapshot=row["is_active"],
                signing_secret_version_snapshot=row["secret_version"],
            )
        )

    op.alter_column("events", "canonical_envelope", nullable=False)
    for column in (
        "endpoint_public_id_snapshot",
        "endpoint_url_snapshot",
        "endpoint_active_snapshot",
        "signing_secret_version_snapshot",
    ):
        op.alter_column("deliveries", column, nullable=False)
    op.create_check_constraint(
        "ck_deliveries_snapshot_secret_version",
        "deliveries",
        "signing_secret_version_snapshot >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_deliveries_snapshot_secret_version",
        "deliveries",
        type_="check",
    )
    op.drop_column("deliveries", "signing_secret_version_snapshot")
    op.drop_column("deliveries", "endpoint_active_snapshot")
    op.drop_column("deliveries", "endpoint_url_snapshot")
    op.drop_column("deliveries", "endpoint_public_id_snapshot")
    op.drop_column("events", "canonical_envelope")
