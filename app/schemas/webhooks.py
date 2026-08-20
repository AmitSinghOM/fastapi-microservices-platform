from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

STRICT = ConfigDict(extra="forbid", strict=True)
ORM = ConfigDict(extra="forbid", strict=True, from_attributes=True)


class OrganizationCreate(BaseModel):
    model_config = STRICT
    name: str = Field(min_length=1, max_length=120)


class OrganizationOut(BaseModel):
    model_config = ORM
    public_id: str
    name: str
    created_at: datetime


class MemberCreate(BaseModel):
    model_config = STRICT
    user_id: int = Field(gt=0)
    role: Literal["owner", "member"] = "member"


class MemberOut(BaseModel):
    model_config = ORM
    user_id: int
    role: Literal["owner", "member"]
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


class ApiKeyCreate(BaseModel):
    model_config = STRICT
    name: str = Field(min_length=1, max_length=120)


class ApiKeyOut(BaseModel):
    model_config = ORM
    public_id: str
    name: str
    key_prefix: str
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


class EventCreate(BaseModel):
    model_config = STRICT
    type: str = Field(min_length=1, max_length=150)
    payload: Any


class EventOut(BaseModel):
    model_config = ORM
    public_id: str
    idempotency_key: str
    event_type: str
    payload: Any
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
    cutoff: datetime
    matched: int
    purged: int
    dry_run: bool
