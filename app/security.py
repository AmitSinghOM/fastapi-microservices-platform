"""Password hashing and access tokens.

Passwords were hashed with a bare ``hashlib.sha256(password)``: no salt, so
precomputed tables apply directly, and a deliberately fast hash, so commodity
GPUs test billions of candidates a second.

bcrypt fixes both. It salts every hash automatically and has a tunable work
factor, so verification stays cheap for one login and stays expensive for an
attacker working through a leaked table.

Existing rows are migrated on next successful login — see
``needs_rehash``/``verify_password``. Legacy hashes remain verifiable so nobody
is locked out, but each one is replaced the first time its owner signs in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from app.config import get_settings

# bcrypt with an explicit cost. 12 is a reasonable 2020s default: roughly
# 200-300 ms per verification on server hardware.
_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)

# Marker for pre-migration hashes so they can be told apart from bcrypt.
LEGACY_SHA256_PREFIX = "sha256$"

ALGORITHM = "HS256"


# ──────────────────────────────────────────────────────────────────
# Passwords
# ──────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Hash a password for storage."""
    _validate_password_length(password)
    return _pwd_context.hash(password)


def verify_password(plain: str, stored: str) -> bool:
    """Check a password against a stored hash.

    Accepts both bcrypt and the legacy unsalted SHA-256 so existing accounts
    keep working. Legacy comparison is constant-time to avoid leaking
    information through timing.
    """
    if not stored:
        return False

    if _is_legacy(stored):
        expected = stored[len(LEGACY_SHA256_PREFIX):] if stored.startswith(
            LEGACY_SHA256_PREFIX) else stored
        candidate = hashlib.sha256(plain.encode()).hexdigest()
        return hmac.compare_digest(candidate, expected)

    try:
        return _pwd_context.verify(plain, stored)
    except ValueError:
        # Malformed hash in the database: fail closed.
        return False


def needs_rehash(stored: str) -> bool:
    """True when a stored hash should be upgraded on next successful login."""
    if not stored or _is_legacy(stored):
        return True
    try:
        return _pwd_context.needs_update(stored)
    except ValueError:
        return True


def _is_legacy(stored: str) -> bool:
    """A bare 64-char hex digest is the old unsalted SHA-256 format."""
    if stored.startswith(LEGACY_SHA256_PREFIX):
        return True
    return len(stored) == 64 and all(
        c in "0123456789abcdef" for c in stored.lower())


def _validate_password_length(password: str) -> None:
    """Reject passwords bcrypt cannot handle correctly.

    bcrypt silently truncates beyond 72 bytes, which would make a long
    password no stronger than its first 72 bytes.
    """
    if not password:
        raise ValueError("Password must not be empty")
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError(
            "Password must be at most 72 bytes; bcrypt truncates beyond that")


# ──────────────────────────────────────────────────────────────────
# Access tokens
# ──────────────────────────────────────────────────────────────────

def create_access_token(
    subject: str | int,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Sign a short-lived access token for ``subject`` (the user id)."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    lifetime = expires_delta or timedelta(
        minutes=settings.access_token_expire_minutes)

    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
        "jti": secrets.token_urlsafe(16),
    }
    if extra_claims:
        # Never let a caller overwrite the security-relevant claims.
        reserved = {"sub", "iat", "nbf", "exp", "jti"}
        payload.update({
            k: v for k, v in extra_claims.items() if k not in reserved})

    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify and decode a token.

    Raises ``jwt.InvalidTokenError`` (or a subclass) on any failure. The
    algorithm is pinned so a token cannot ask to be verified with ``none`` or
    with an asymmetric key confusion trick.
    """
    settings = get_settings()
    return jwt.decode(
        token,
        settings.secret_key,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
