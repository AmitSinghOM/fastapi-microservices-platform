"""Initial users, items, and webhook delivery schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000)),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price >= 0", name="ck_items_price_nonnegative"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_items_title", "items", ["title"])
    op.create_index(
        "ix_items_owner_created", "items", ["owner_id", "created_at", "id"]
    )
    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(
        "ix_organizations_public_id",
        "organizations",
        ["public_id"],
        unique=True,
    )
    op.create_table(
        "organization_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'member')", name="ck_org_members_role"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_org_members_org_user"
        ),
    )
    op.create_index(
        "ix_org_members_user_org",
        "organization_members",
        ["user_id", "organization_id"],
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "organization_id", "name", name="uq_projects_name"
        ),
    )
    op.create_index(
        "ix_projects_public_id", "projects", ["public_id"], unique=True
    )
    op.create_index(
        "ix_projects_org_created",
        "projects",
        ["organization_id", "created_at"],
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("key_prefix"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(
        "ix_api_keys_public_id", "api_keys", ["public_id"], unique=True
    )
    op.create_index(
        "ix_api_keys_project_created", "api_keys", ["project_id", "created_at"]
    )
    op.create_index(
        "ix_api_keys_prefix_active", "api_keys", ["key_prefix", "is_active"]
    )
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("description", sa.String(500)),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("secret_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "secret_version >= 1", name="ck_webhook_endpoints_secret_version"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(
        "ix_webhook_endpoints_public_id",
        "webhook_endpoints",
        ["public_id"],
        unique=True,
    )
    op.create_index(
        "ix_webhook_endpoints_project_active",
        "webhook_endpoints",
        ["project_id", "is_active"],
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(150), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_events_idempotency"
        ),
    )
    op.create_index(
        "ix_events_public_id", "events", ["public_id"], unique=True
    )
    op.create_index(
        "ix_events_project_created",
        "events",
        ["project_id", "created_at", "id"],
    )
    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("replay_of_delivery_id", sa.Integer()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_http_status", sa.Integer()),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'succeeded', 'dead')",
            name="ck_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_deliveries_attempt_count"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["webhook_endpoints.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["replay_of_delivery_id"], ["deliveries.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index(
        "ix_deliveries_public_id", "deliveries", ["public_id"], unique=True
    )
    op.create_index(
        "ix_deliveries_due",
        "deliveries",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.create_index(
        "ix_deliveries_event", "deliveries", ["event_id", "created_at"]
    )
    op.create_index(
        "ix_deliveries_endpoint", "deliveries", ["endpoint_id", "created_at"]
    )

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error", sa.String(500)),
        sa.Column("response_body", sa.Text()),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["deliveries.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "delivery_id", "attempt_number", name="uq_attempts_delivery_number"
        ),
    )
    op.create_index(
        "ix_attempts_delivery_started",
        "delivery_attempts",
        ["delivery_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("delivery_attempts")
    op.drop_table("deliveries")
    op.drop_table("events")
    op.drop_table("webhook_endpoints")
    op.drop_table("api_keys")
    op.drop_table("projects")
    op.drop_table("organization_members")
    op.drop_table("organizations")
    op.drop_table("items")
    op.drop_table("users")
