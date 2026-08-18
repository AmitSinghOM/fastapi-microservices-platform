"""Remove uniqueness duplicated by named unique indexes."""

from alembic import op

revision = "0002_drop_duplicate_uniques"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_CONSTRAINTS = (
    ("users", "users_email_key", "email"),
    ("organizations", "organizations_public_id_key", "public_id"),
    ("projects", "projects_public_id_key", "public_id"),
    ("api_keys", "api_keys_public_id_key", "public_id"),
    (
        "webhook_endpoints",
        "webhook_endpoints_public_id_key",
        "public_id",
    ),
    ("events", "events_public_id_key", "public_id"),
    ("deliveries", "deliveries_public_id_key", "public_id"),
)


def upgrade() -> None:
    for table, constraint, _ in _CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="unique")


def downgrade() -> None:
    for table, constraint, column in reversed(_CONSTRAINTS):
        op.create_unique_constraint(constraint, table, [column])
