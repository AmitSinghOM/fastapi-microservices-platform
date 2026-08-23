from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

STRICT = ConfigDict(extra="forbid", strict=True)
ORM = ConfigDict(extra="forbid", strict=True, from_attributes=True)

OrganizationRole = Literal["owner", "admin", "member"]
ProjectRole = Literal["admin", "operator", "viewer"]
ApiKeyScope = Literal["events:write"]
Plan = Literal["free", "standard", "enterprise"]


def default_api_key_scopes() -> list[ApiKeyScope]:
    return ["events:write"]


class OrganizationCreate(BaseModel):
    model_config = STRICT
    name: str = Field(min_length=1, max_length=120)


class OrganizationOut(BaseModel):
    model_config = ORM
    public_id: str
    name: str
    lifecycle_state: Literal["active", "deletion_pending"]
    deletion_requested_at: datetime | None
    deletion_scheduled_at: datetime | None
    created_at: datetime


class MemberCreate(BaseModel):
    model_config = STRICT
    user_id: int = Field(gt=0)
    role: OrganizationRole = "member"


class MemberUpdate(BaseModel):
    model_config = STRICT
    role: OrganizationRole


class MemberOut(BaseModel):
    model_config = ORM
    user_id: int
    role: OrganizationRole
    created_at: datetime


class ProjectCreate(BaseModel):
    model_config = STRICT
    name: str = Field(min_length=1, max_length=120)


class ProjectOut(BaseModel):
    model_config = ORM
    public_id: str
    name: str
    is_active: bool
    created_at: datetime


class ProjectMemberCreate(BaseModel):
    model_config = STRICT
    user_id: int = Field(gt=0)
    role: ProjectRole = "viewer"


class ProjectMemberUpdate(BaseModel):
    model_config = STRICT
    role: ProjectRole


class ProjectMemberOut(BaseModel):
    model_config = ORM
    user_id: int
    role: ProjectRole
    created_at: datetime


class ApiKeyCreate(BaseModel):
    model_config = STRICT
    name: str = Field(min_length=1, max_length=120)
    scopes: list[ApiKeyScope] = Field(
        default_factory=default_api_key_scopes, min_length=1, max_length=16
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=3_650)

    @model_validator(mode="after")
    def require_unique_scopes(self) -> "ApiKeyCreate":
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("scopes must be unique")
        return self


class ApiKeyRotateRequest(BaseModel):
    model_config = STRICT
    overlap_seconds: int | None = Field(default=None, ge=1, le=86_400)


class ApiKeyOut(BaseModel):
    model_config = ORM
    public_id: str
    name: str
    key_prefix: str
    scopes: list[str]
    expires_at: datetime | None
    rotation_family_id: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    plaintext_key: str


class EndpointCreate(BaseModel):
    model_config = STRICT
    url: HttpUrl
    description: str | None = Field(default=None, max_length=500)


class EndpointUpdate(BaseModel):
    model_config = STRICT
    url: HttpUrl | None = None
    description: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_valid_change(self) -> "EndpointUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        for field_name in ("url", "is_active"):
            if field_name in self.model_fields_set and getattr(
                self, field_name
            ) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class EndpointOut(BaseModel):
    model_config = ORM
    public_id: str
    url: str
    description: str | None
    is_active: bool
    secret_version: int
    created_at: datetime
    updated_at: datetime


class EndpointCreated(EndpointOut):
    signing_secret: str


class EndpointSecretRotated(BaseModel):
    model_config = STRICT
    public_id: str
    secret_version: int
    signing_secret: str
    previous_valid_until: datetime | None


class EventCreate(BaseModel):
    model_config = STRICT
    type: str = Field(min_length=1, max_length=150)
    payload: Any


class EventOut(BaseModel):
    model_config = ORM
    public_id: str
    idempotency_key: str
    event_type: str
    payload: Any | None
    payload_purged_at: datetime | None
    created_at: datetime


DeliveryStatus = Literal[
    "pending",
    "processing",
    "retry_scheduled",
    "succeeded",
    "dead",
    "canceled",
]


class DeliveryOut(BaseModel):
    model_config = ORM
    public_id: str
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    last_http_status: int | None
    last_error: str | None
    replay_of_delivery_id: int | None
    created_at: datetime
    updated_at: datetime
    succeeded_at: datetime | None
    dead_at: datetime | None
    dead_reason: str | None
    canceled_at: datetime | None
    canceled_reason: str | None


class DeliveryAttemptOut(BaseModel):
    model_config = ORM
    attempt_number: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    http_status: int | None
    error: str | None
    response_body: str | None
    response_purged_at: datetime | None


class DeliveryDetail(DeliveryOut):
    attempts: list[DeliveryAttemptOut]


class ReplayOut(BaseModel):
    model_config = ORM
    public_id: str
    status: DeliveryStatus
    replay_of_delivery_id: int
    created_at: datetime


class ReplayBatchRequest(BaseModel):
    model_config = STRICT
    delivery_ids: list[str] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def require_unique_ids(self) -> "ReplayBatchRequest":
        if len(set(self.delivery_ids)) != len(self.delivery_ids):
            raise ValueError("delivery_ids must be unique")
        return self


class ReplayOperationOut(BaseModel):
    model_config = ORM
    public_id: str
    idempotency_key: str
    mode: Literal["single", "bulk"]
    requested_count: int
    created_count: int
    source_delivery_ids: list[str]
    created_delivery_ids: list[str]
    created_at: datetime


class EndpointPauseRequest(BaseModel):
    model_config = STRICT
    reason: str | None = Field(default=None, max_length=200)


class EndpointRuntimeOut(BaseModel):
    model_config = STRICT
    endpoint_id: str
    paused: bool
    pause_reason: str | None
    circuit_state: Literal["closed", "open", "half_open"]
    consecutive_failures: int
    circuit_open_until: datetime | None


class DeliveryCancelRequest(BaseModel):
    model_config = STRICT
    reason: str | None = Field(default=None, max_length=200)


class DeliveryPurgeRequest(BaseModel):
    model_config = STRICT
    dry_run: bool = True
    max_records: int = Field(default=500, ge=1, le=10_000)


class DeliveryPurgeOut(BaseModel):
    model_config = STRICT
    cutoff: datetime | None
    matched: int
    purged: int
    dry_run: bool


class OrganizationPolicyUpdate(BaseModel):
    model_config = STRICT
    plan: Plan | None = None
    payload_retention_days: int | None = Field(
        default=None, ge=1, le=3_650
    )
    response_retention_days: int | None = Field(
        default=None, ge=1, le=3_650
    )

    @model_validator(mode="after")
    def require_valid_change(self) -> "OrganizationPolicyUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        for field_name in self.model_fields_set:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class OrganizationPolicyOut(BaseModel):
    model_config = ORM
    plan: Plan
    payload_retention_days: int
    response_retention_days: int
    created_at: datetime
    updated_at: datetime


class AuditEventOut(BaseModel):
    model_config = ORM
    id: int
    public_id: str
    organization_public_id: str
    project_public_id: str | None
    actor_user_id: int | None
    action: str
    resource_type: str
    resource_public_id: str | None
    sanitized_metadata: dict[str, Any]
    created_at: datetime


class OrganizationLifecycleOut(BaseModel):
    model_config = ORM
    public_id: str
    kind: Literal["deletion"]
    status: Literal["pending", "running", "completed", "canceled", "failed"]
    scheduled_at: datetime
    created_at: datetime
    completed_at: datetime | None
    error: str | None


class OrganizationExportProject(BaseModel):
    model_config = STRICT
    public_id: str
    name: str
    is_active: bool
    created_at: datetime


class OrganizationExportApiKey(BaseModel):
    model_config = STRICT
    public_id: str
    project_public_id: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    status: Literal["active", "expired", "revoked"]
    created_at: datetime


class OrganizationExportEndpoint(BaseModel):
    model_config = STRICT
    public_id: str
    project_public_id: str
    description: str | None
    is_active: bool
    signing_version: int
    created_at: datetime
    updated_at: datetime


class OrganizationExportCounts(BaseModel):
    model_config = STRICT
    members: int = Field(ge=0)
    projects: int = Field(ge=0)
    project_members: int = Field(ge=0)
    api_keys: int = Field(ge=0)
    endpoints: int = Field(ge=0)
    events: int = Field(ge=0)
    deliveries: int = Field(ge=0)
    attempts: int = Field(ge=0)


class OrganizationExportOut(BaseModel):
    model_config = STRICT
    generated_at: datetime
    organization: OrganizationOut
    policy: OrganizationPolicyOut
    lifecycle: OrganizationLifecycleOut | None
    counts: OrganizationExportCounts
    members: list[MemberOut]
    projects: list[OrganizationExportProject]
    api_keys: list[OrganizationExportApiKey]
    endpoints: list[OrganizationExportEndpoint]
    audit_events: list[AuditEventOut]
    truncated: bool
