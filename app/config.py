import secrets
import sys
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "FastAPI Microservices Platform"
    debug: bool = False
    database_url: str = "sqlite+aiosqlite:///./app.db"

    # Signing key for access tokens. There is deliberately no usable default:
    # a shipped default secret is the same as no secret, since anyone with the
    # source can mint tokens. Generated per-process when unset, which keeps
    # local development working while making tokens useless across restarts.
    secret_key: str = ""

    access_token_expire_minutes: int = 30

    # Requests per window, per client IP, on unauthenticated endpoints.
    rate_limit_requests: int = 20
    rate_limit_window_seconds: int = 60

    class Config:
        env_file = ".env"

    @field_validator("secret_key")
    @classmethod
    def _require_secret_in_production(cls, value: str, info) -> str:
        if value:
            if len(value) < 32:
                raise ValueError(
                    "SECRET_KEY must be at least 32 characters. Generate one "
                    "with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
                )
            return value

        # Unset. Fine for tests and local runs, never for a real deployment.
        generated = secrets.token_urlsafe(32)
        print(
            "WARNING: SECRET_KEY is not set. Using a random key generated for "
            "this process only — all tokens become invalid on restart. Set "
            "SECRET_KEY in the environment before deploying.",
            file=sys.stderr,
        )
        return generated


@lru_cache
def get_settings() -> Settings:
    return Settings()
