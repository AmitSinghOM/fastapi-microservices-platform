import secrets
import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application and webhook worker configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_name: str = "FastAPI Webhook Platform"
    app_version: str = "3.0.0"
    environment: Literal[
        "development", "test", "staging", "production"
    ] = "development"
    debug: bool = False
    docs_enabled: bool = True
    example_items_enabled: bool = True
    database_url: str = "sqlite+aiosqlite:///./app.db"
    auto_create_schema: bool = True
    database_health_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    api_db_pool_size: int = Field(default=5, ge=1, le=100)
    api_db_max_overflow: int = Field(default=5, ge=0, le=100)
    api_db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    worker_db_pool_size: int = Field(default=5, ge=1, le=100)
    worker_db_max_overflow: int = Field(default=5, ge=0, le=100)
    worker_db_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    secret_key: str = ""
    api_key_pepper: str = ""
    webhook_signing_key: str = ""
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    allow_http_webhooks: bool | None = None

    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "test", "testserver"]
    cors_origins: list[str] = []
    rate_limit_requests: int = Field(default=20, ge=1, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=86_400)
    rate_limit_max_entries: int = Field(default=10_000, ge=100, le=1_000_000)

    worker_poll_seconds: float = Field(default=1.0, ge=0.05, le=60)
    worker_batch_size: int = Field(default=50, ge=1, le=500)
    worker_concurrency: int = Field(default=10, ge=1, le=100)
    worker_lease_seconds: int = Field(default=60, ge=5, le=3600)
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0, le=600)
    worker_attempt_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    worker_finalization_margin_seconds: float = Field(
        default=5.0, gt=0, le=60
    )
    worker_shutdown_grace_seconds: float = Field(default=45.0, gt=0, le=600)
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    http_read_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    http_write_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    http_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    webhook_payload_max_bytes: int = Field(
        default=262_144,
        ge=1,
        le=10_485_760,
    )
    webhook_response_max_bytes: int = Field(
        default=16_384,
        ge=0,
        le=1_048_576,
    )
    idempotency_key_max_length: int = Field(default=255, ge=8, le=1024)
    webhook_max_attempts: int = Field(default=8, ge=1, le=50)
    webhook_backoff_base_seconds: float = Field(default=1.0, gt=0, le=3600)
    webhook_backoff_cap_seconds: float = Field(default=3600.0, gt=0, le=86_400)
    api_key_usage_flush_seconds: float = Field(default=30.0, gt=0, le=600)
    api_key_usage_max_entries: int = Field(
        default=10_000, ge=100, le=1_000_000
    )

    @model_validator(mode="after")
    def validate_deployment_settings(self) -> "Settings":
        deployed = self.environment in {"staging", "production"}
        for field_name, label in (
            ("secret_key", "SECRET_KEY"),
            ("api_key_pepper", "API_KEY_PEPPER"),
            ("webhook_signing_key", "WEBHOOK_SIGNING_KEY"),
        ):
            value = getattr(self, field_name)
            if not value:
                if deployed:
                    raise ValueError(f"{label} is required when deployed")
                setattr(self, field_name, secrets.token_urlsafe(32))
                print(
                    f"WARNING: {label} is unset; using a process-local "
                    "development/test key.",
                    file=sys.stderr,
                )
            elif len(value) < 32:
                raise ValueError(f"{label} must be at least 32 characters")

        if self.allow_http_webhooks is None:
            self.allow_http_webhooks = self.environment == "development"
        elif self.allow_http_webhooks and self.environment != "development":
            raise ValueError(
                "ALLOW_HTTP_WEBHOOKS may be true only in development"
            )
        if (
            self.webhook_backoff_cap_seconds
            < self.webhook_backoff_base_seconds
        ):
            raise ValueError(
                "WEBHOOK_BACKOFF_CAP_SECONDS must be at least the base"
            )
        required_lease = (
            self.worker_attempt_timeout_seconds
            + self.worker_heartbeat_seconds
            + self.worker_finalization_margin_seconds
        )
        if self.worker_lease_seconds <= required_lease:
            raise ValueError(
                "WORKER_LEASE_SECONDS must exceed the attempt deadline, "
                "heartbeat delay, and finalization margin"
            )
        if self.worker_shutdown_grace_seconds < (
            self.worker_attempt_timeout_seconds
            + self.worker_finalization_margin_seconds
        ):
            raise ValueError(
                "WORKER_SHUTDOWN_GRACE_SECONDS must cover the attempt "
                "deadline and finalization margin"
            )
        if deployed and self.auto_create_schema:
            raise ValueError(
                "AUTO_CREATE_SCHEMA must be false when deployed; "
                "run migrations"
            )
        if self.environment == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        if deployed and not self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must not be empty when deployed")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
