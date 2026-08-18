from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
    name = Column(String(120), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    members = relationship(
        "OrganizationMember", cascade="all, delete-orphan"
    )
    projects = relationship("Project", cascade="all, delete-orphan")


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_org_members_org_user"
        ),
        CheckConstraint(
            "role IN ('owner', 'member')", name="ck_org_members_role"
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
    api_keys = relationship("ApiKey", cascade="all, delete-orphan")
    endpoints = relationship(
        "WebhookEndpoint", cascade="all, delete-orphan"
    )
    events = relationship("Event", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_project_created", "project_id", "created_at"),
        Index("ix_api_keys_prefix_active", "key_prefix", "is_active"),
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
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    project = relationship("Project", overlaps="api_keys")


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


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "idempotency_key", name="uq_events_idempotency"
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
    payload = Column(JSON, nullable=False)
    payload_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    project = relationship("Project", overlaps="events")
    deliveries = relationship("Delivery", cascade="all, delete-orphan")


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'retry_scheduled', "
            "'succeeded', 'dead')",
            name="ck_deliveries_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_deliveries_attempt_count"
        ),
        Index(
            "ix_deliveries_due",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
        Index("ix_deliveries_event", "event_id", "created_at"),
        Index("ix_deliveries_endpoint", "endpoint_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, index=True, nullable=False)
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

    event = relationship("Event", overlaps="deliveries")
    endpoint = relationship("WebhookEndpoint", overlaps="deliveries")
    replay_of = relationship("Delivery", remote_side=[id])
    attempts = relationship("DeliveryAttempt", cascade="all, delete-orphan")


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

    delivery = relationship("Delivery", overlaps="attempts")
