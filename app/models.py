from sqlalchemy import (
    JSON,
    DDL,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    items = relationship(
        "Item",
        back_populates="owner",
        cascade="all, delete-orphan",
    )


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_items_price_nonnegative"),
        Index("ix_items_owner_created", "owner_id", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String(200), index=True, nullable=False)
    description = Column(String(2_000), nullable=True)
    price = Column(Numeric(12, 2), nullable=False)
    owner_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), nullable=False)

    owner = relationship("User", back_populates="items")


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('active', 'deletion_pending')",
            name="ck_organizations_lifecycle_state",
        ),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    name = Column(String(120), nullable=False)
    lifecycle_state = Column(
        String(24), default="active", server_default="active", nullable=False
    )
    deletion_requested_at = Column(DateTime(timezone=True), nullable=True)
    deletion_scheduled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    members = relationship(
        "OrganizationMember", cascade="all, delete-orphan"
    )
    projects = relationship("Project", cascade="all, delete-orphan")
    policy = relationship(
        "OrganizationPolicy", cascade="all, delete-orphan", uselist=False
    )
    lifecycle_operations = relationship(
        "OrganizationLifecycleOperation",
        cascade="all, delete-orphan",
    )
    quota_state = relationship(
        "TenantQuotaState", cascade="all, delete-orphan", uselist=False
    )


class OrganizationPolicy(Base):
    __tablename__ = "organization_policies"
    __table_args__ = (
        CheckConstraint(
            "plan IN ('free', 'standard', 'enterprise')",
            name="ck_organization_policies_plan",
        ),
        CheckConstraint(
            "payload_retention_days > 0",
            name="ck_organization_policies_payload_retention",
        ),
        CheckConstraint(
            "response_retention_days > 0",
            name="ck_organization_policies_response_retention",
        ),
    )

    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan = Column(
        String(16), default="free", server_default="free", nullable=False
    )
    payload_retention_days = Column(
        Integer, default=30, server_default="30", nullable=False
    )
    response_retention_days = Column(
        Integer, default=30, server_default="30", nullable=False
    )
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    organization = relationship("Organization", overlaps="policy")


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_org_members_org_user"
        ),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_org_members_role",
        ),
        Index("ix_org_members_user_org", "user_id", "organization_id"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    organization = relationship("Organization", overlaps="members")
    user = relationship("User")


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_projects_name"),
        Index("ix_projects_org_created", "organization_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(120), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    organization = relationship("Organization", overlaps="projects")
    members = relationship("ProjectMember", cascade="all, delete-orphan")
    api_keys = relationship("ApiKey", cascade="all, delete-orphan")
    endpoints = relationship(
        "WebhookEndpoint", cascade="all, delete-orphan"
    )
    events = relationship("Event", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "user_id", name="uq_project_members_project_user"
        ),
        CheckConstraint(
            "role IN ('admin', 'operator', 'viewer')",
            name="ck_project_members_role",
        ),
        Index("ix_project_members_user_project", "user_id", "project_id"),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    project = relationship("Project", overlaps="members")
    user = relationship("User")


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_project_created", "project_id", "created_at"),
        Index("ix_api_keys_prefix_active", "key_prefix", "is_active"),
        Index("ix_api_keys_expiry_active", "expires_at", "is_active"),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(120), nullable=False)
    key_prefix = Column(String(24), unique=True, nullable=False)
    key_digest = Column(String(64), nullable=False)
    scopes = Column(
        JSON,
        default=lambda: ["events:write"],
        server_default=text("'[\"events:write\"]'"),
        nullable=False,
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    rotation_family_id = Column(
        String(36),
        default=lambda context: context.get_current_parameters()["public_id"],
        nullable=False,
    )
    rotated_from_id = Column(
        Integer,
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", overlaps="api_keys")
    rotated_from = relationship("ApiKey", remote_side=[id])


class GlobalControlState(Base):
    __tablename__ = "global_control_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_global_control_singleton"),
    )

    id = Column(Integer, primary_key=True)
    tenant_cursor_organization_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class TenantQuotaState(Base):
    __tablename__ = "tenant_quota_state"

    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    event_tokens = Column(Float, nullable=False)
    event_refilled_at = Column(DateTime(timezone=True), nullable=False)
    delivery_tokens = Column(Float, nullable=False)
    delivery_refilled_at = Column(DateTime(timezone=True), nullable=False)
    replay_tokens = Column(Float, nullable=False)
    replay_refilled_at = Column(DateTime(timezone=True), nullable=False)
    endpoint_cursor_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    organization = relationship("Organization", overlaps="quota_state")


class EndpointQuotaState(Base):
    __tablename__ = "endpoint_quota_state"
    __table_args__ = (
        CheckConstraint(
            "circuit_state IN ('closed', 'open', 'half_open')",
            name="ck_endpoint_quota_circuit_state",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_endpoint_quota_failures_nonnegative",
        ),
    )

    endpoint_id = Column(
        Integer,
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        primary_key=True,
    )
    delivery_tokens = Column(Float, nullable=False)
    refilled_at = Column(DateTime(timezone=True), nullable=False)
    retry_tokens = Column(Float, nullable=False)
    retry_refilled_at = Column(DateTime(timezone=True), nullable=False)
    circuit_state = Column(String(16), nullable=False)
    consecutive_failures = Column(Integer, nullable=False)
    circuit_open_until = Column(DateTime(timezone=True), nullable=True)
    half_open_probe_delivery_id = Column(
        Integer,
        ForeignKey("deliveries.id", ondelete="SET NULL"),
        nullable=True,
    )
    paused_at = Column(DateTime(timezone=True), nullable=True)
    pause_reason = Column(String(200), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    endpoint = relationship("WebhookEndpoint", overlaps="quota_state")


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        CheckConstraint(
            "secret_version >= 1", name="ck_webhook_endpoints_secret_version"
        ),
        Index(
            "ix_webhook_endpoints_project_active",
            "project_id",
            "is_active",
        ),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    url = Column(String(2_048), nullable=False)
    description = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    secret_version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    project = relationship("Project", overlaps="endpoints")
    deliveries = relationship("Delivery", cascade="all, delete-orphan")
    quota_state = relationship(
        "EndpointQuotaState", cascade="all, delete-orphan", uselist=False
    )
    signing_secret_versions = relationship(
        "EndpointSigningSecretVersion",
        cascade="all, delete-orphan",
    )


class EndpointSigningSecretVersion(Base):
    __tablename__ = "endpoint_signing_secret_versions"
    __table_args__ = (
        UniqueConstraint(
            "endpoint_id",
            "version",
            name="uq_endpoint_signing_secrets_endpoint_version",
        ),
        CheckConstraint(
            "version >= 1", name="ck_endpoint_signing_secrets_version"
        ),
        Index(
            "ix_endpoint_signing_secrets_retirement",
            "endpoint_id",
            "retire_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    endpoint_id = Column(
        Integer,
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(Integer, nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    retire_at = Column(DateTime(timezone=True), nullable=True)

    endpoint = relationship(
        "WebhookEndpoint", overlaps="signing_secret_versions"
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_events_idempotency"
        ),
        CheckConstraint(
            "envelope_mode IN ('native', 'cloudevents')",
            name="ck_events_envelope_mode",
        ),
        Index("ix_events_project_created", "project_id", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key = Column(String(255), nullable=False)
    event_type = Column(String(150), nullable=False)
    envelope_mode = Column(
        String(16), nullable=False, default="native", server_default="native"
    )
    payload = Column(JSON, nullable=True)
    payload_hash = Column(String(64), nullable=False)
    canonical_envelope = Column(LargeBinary, nullable=True)
    payload_purged_at = Column(DateTime(timezone=True), nullable=True)
    traceparent = Column(String(55))
    tracestate = Column(String(512))
    created_at = Column(DateTime(timezone=True), nullable=False)

    project = relationship("Project", overlaps="events")
    deliveries = relationship("Delivery", cascade="all, delete-orphan")


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'succeeded', 'dead', 'canceled')",
            name="ck_deliveries_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_deliveries_attempt_count"
        ),
        CheckConstraint(
            "signing_secret_version_snapshot >= 1",
            name="ck_deliveries_snapshot_secret_version",
        ),
        Index(
            "ix_deliveries_due",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index("ix_deliveries_event", "event_id", "created_at"),
        Index("ix_deliveries_endpoint", "endpoint_id", "created_at"),
        Index(
            "ix_deliveries_dead_operations",
            "organization_id",
            "status",
            "dead_at",
            "id",
        ),
        Index(
            "ix_deliveries_endpoint_dead",
            "endpoint_id",
            "status",
            "dead_at",
            "id",
        ),
        Index(
            "ix_deliveries_fair_due",
            "organization_id",
            "endpoint_id",
            "status",
            "next_attempt_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = Column(
        Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id = Column(
        Integer,
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    replay_of_delivery_id = Column(
        Integer,
        ForeignKey("deliveries.id", ondelete="SET NULL"),
        nullable=True,
    )
    endpoint_public_id_snapshot = Column(String(36), nullable=False)
    endpoint_url_snapshot = Column(String(2_048), nullable=False)
    endpoint_active_snapshot = Column(Boolean, nullable=False)
    signing_secret_version_snapshot = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False)
    attempt_count = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False)
    lease_token = Column(String(36), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_http_status = Column(Integer, nullable=True)
    last_error = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    succeeded_at = Column(DateTime(timezone=True), nullable=True)
    dead_at = Column(DateTime(timezone=True), nullable=True)
    dead_reason = Column(String(64), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    canceled_reason = Column(String(200), nullable=True)

    event = relationship("Event", overlaps="deliveries")
    endpoint = relationship("WebhookEndpoint", overlaps="deliveries")
    replay_of = relationship("Delivery", remote_side=[id])
    attempts = relationship("DeliveryAttempt", cascade="all, delete-orphan")


class ReplayOperation(Base):
    __tablename__ = "replay_operations"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_replay_operations_project_key",
        ),
        CheckConstraint(
            "mode IN ('single', 'bulk')",
            name="ck_replay_operations_mode",
        ),
        Index(
            "ix_replay_operations_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id = Column(
        Integer,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key = Column(String(255), nullable=False)
    mode = Column(String(16), nullable=False)
    requested_count = Column(Integer, nullable=False)
    created_count = Column(Integer, nullable=False)
    source_delivery_ids = Column(JSON, nullable=False)
    created_delivery_ids = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class OrganizationLifecycleOperation(Base):
    __tablename__ = "organization_lifecycle_operations"
    __table_args__ = (
        CheckConstraint(
            "kind = 'deletion'",
            name="ck_org_lifecycle_operations_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'canceled', "
            "'failed')",
            name="ck_org_lifecycle_operations_status",
        ),
        Index(
            "ix_org_lifecycle_operations_org_created",
            "organization_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_org_lifecycle_operations_status_scheduled",
            "status",
            "scheduled_at",
            "id",
        ),
        Index(
            "uq_org_lifecycle_operations_active",
            "organization_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(String(500), nullable=True)

    organization = relationship(
        "Organization", overlaps="lifecycle_operations"
    )
    requested_by_user = relationship("User")


class AdministrativeAuditEvent(Base):
    __tablename__ = "administrative_audit_events"
    __table_args__ = (
        Index(
            "ix_administrative_audit_events_org_created",
            "organization_public_id",
            "created_at",
            "id",
        ),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    organization_public_id = Column(String(36), nullable=False)
    project_public_id = Column(String(36), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    action = Column(String(100), nullable=False)
    resource_type = Column(String(100), nullable=False)
    resource_public_id = Column(String(36), nullable=True)
    sanitized_metadata = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


_AUDIT_IMMUTABILITY_FUNCTION = DDL("""
CREATE FUNCTION reject_administrative_audit_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'administrative audit events are immutable';
END;
$$ LANGUAGE plpgsql
""").execute_if(dialect="postgresql")
_AUDIT_IMMUTABILITY_TRIGGER = DDL("""
CREATE TRIGGER trg_administrative_audit_events_immutable
BEFORE UPDATE OR DELETE ON administrative_audit_events
FOR EACH ROW
EXECUTE FUNCTION reject_administrative_audit_event_mutation()
""").execute_if(dialect="postgresql")
event.listen(
    AdministrativeAuditEvent.__table__,
    "after_create",
    _AUDIT_IMMUTABILITY_FUNCTION,
)
event.listen(
    AdministrativeAuditEvent.__table__,
    "after_create",
    _AUDIT_IMMUTABILITY_TRIGGER,
)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id", "attempt_number", name="uq_attempts_delivery_number"
        ),
        Index("ix_attempts_delivery_started", "delivery_id", "started_at"),
    )

    id = Column(Integer, primary_key=True)
    delivery_id = Column(
        Integer,
        ForeignKey("deliveries.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number = Column(Integer, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=False)
    outcome = Column(String(32), nullable=False)
    http_status = Column(Integer, nullable=True)
    error = Column(String(500), nullable=True)
    response_body = Column(Text, nullable=True)
    response_purged_at = Column(DateTime(timezone=True), nullable=True)

    delivery = relationship("Delivery", overlaps="attempts")
