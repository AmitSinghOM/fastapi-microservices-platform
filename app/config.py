import secrets
import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FastAPI Microservices Platform"
    app_version: str = "2.0.0"
    environment: Literal[
        "development", "test", "staging", "production"
    ] = "development"
    debug: bool = False
    docs_enabled: bool = True

    database_url: str = "sqlite+aiosqlite:///./app.db"
    auto_create_schema: bool = True
    database_health_timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    secret_key: str = ""
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)

    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "test", "testserver"]
    cors_origins: list[str] = []

    rate_limit_requests: int = Field(default=20, ge=1, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=86_400)
    rate_limit_max_entries: int = Field(default=10_000, ge=100, le=1_000_000)

    @model_validator(mode="after")
    def validate_deployment_settings(self) -> "Settings":
        deployed = self.environment in {"staging", "production"}

        if not self.secret_key:
            if deployed:
                raise ValueError(
                    "SECRET_KEY is required in staging and production"
                )
            self.secret_key = secrets.token_urlsafe(32)
            print(
                "WARNING: SECRET_KEY is unset; using a process-local key. "
                "Set SECRET_KEY before deploying.",
                file=sys.stderr,
            )
        elif len(self.secret_key) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")

        if deployed and self.auto_create_schema:
            raise ValueError(
                "AUTO_CREATE_SCHEMA must be false in staging and production; "
                "apply versioned database migrations during deployment"
            )
        if self.environment == "production" and self.debug:
            raise ValueError("DEBUG must be false in production")
        if deployed and not self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must not be empty when deployed")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
