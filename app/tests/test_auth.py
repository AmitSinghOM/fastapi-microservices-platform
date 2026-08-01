"""Authentication: login, tokens, and password storage."""

from datetime import timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models import User
from app.security import (
    LEGACY_SHA256_PREFIX,
    create_access_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.tests.conftest import register_and_login


# ── password storage ──────────────────────────────────────────────

def test_hash_is_salted_so_identical_passwords_differ():
    """Unsalted SHA-256 gave the same digest for the same password, so one
    rainbow table cracked every account that shared a password."""
    first = hash_password("same password")
    second = hash_password("same password")

    assert first != second
    assert verify_password("same password", first)
    assert verify_password("same password", second)


def test_hash_is_not_a_bare_sha256_digest():
    hashed = hash_password("password123")

    assert hashed.startswith("$2b$")
    assert len(hashed) != 64


def test_wrong_password_is_rejected():
    hashed = hash_password("correct")

    assert verify_password("wrong", hashed) is False
    assert verify_password("", hashed) is False


def test_empty_and_overlong_passwords_are_refused():
    with pytest.raises(ValueError):
        hash_password("")
    # bcrypt silently truncates past 72 bytes, which would make the tail of a
    # long passphrase contribute nothing.
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password("a" * 73)


def test_legacy_sha256_hash_still_verifies():
    """Existing accounts must not be locked out by the migration."""
    import hashlib
    legacy = hashlib.sha256(b"oldpassword").hexdigest()

    assert verify_password("oldpassword", legacy) is True
    assert verify_password("wrong", legacy) is False
    assert needs_rehash(legacy) is True


def test_prefixed_legacy_hash_still_verifies():
    import hashlib
    legacy = LEGACY_SHA256_PREFIX + hashlib.sha256(b"oldpassword").hexdigest()

    assert verify_password("oldpassword", legacy) is True


def test_bcrypt_hash_does_not_need_rehash():
    assert needs_rehash(hash_password("password123")) is False


def test_malformed_stored_hash_fails_closed():
    assert verify_password("anything", "not-a-hash") is False
    assert verify_password("anything", "") is False


# ── tokens ────────────────────────────────────────────────────────

def test_token_round_trips():
    from app.security import decode_access_token

    token = create_access_token(subject=42)
    payload = decode_access_token(token)

    assert payload["sub"] == "42"
    assert "exp" in payload and "jti" in payload


def test_expired_token_is_rejected():
    from app.security import decode_access_token

    token = create_access_token(subject=1, expires_delta=timedelta(seconds=-1))

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_token_signed_with_another_key_is_rejected():
    from app.security import ALGORITHM, decode_access_token

    forged = jwt.encode(
        {"sub": "1", "exp": 9999999999}, "attacker-key", algorithm=ALGORITHM)

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(forged)


def test_unsigned_none_algorithm_token_is_rejected():
    """alg=none is the classic JWT bypass; the algorithm list is pinned."""
    from app.security import decode_access_token

    unsigned = jwt.encode({"sub": "1", "exp": 9999999999}, key="", algorithm="none")

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(unsigned)


def test_caller_cannot_override_reserved_claims():
    from app.security import decode_access_token

    token = create_access_token(
        subject=7, extra_claims={"sub": "1", "exp": 9999999999, "role": "admin"})
    payload = decode_access_token(token)

    assert payload["sub"] == "7"
    assert payload["role"] == "admin"


# ── login endpoint ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_returns_a_usable_token(client: AsyncClient):
    _, token = await register_and_login(client, "login@example.com")

    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "login@example.com"


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: AsyncClient
):
    """Different messages would turn login into an email enumeration oracle."""
    await client.post(
        "/users/",
        json={"email": "real@example.com", "name": "R", "password": "rightpassword"})

    wrong_password = await client.post(
        "/auth/login",
        data={"username": "real@example.com", "password": "wrongpassword"})
    unknown_email = await client.post(
        "/auth/login",
        data={"username": "ghost@example.com", "password": "wrongpassword"})

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


@pytest.mark.asyncio
async def test_login_is_rate_limited(client: AsyncClient):
    """Was unbounded: unlimited password guesses against a known email."""
    await client.post(
        "/users/",
        json={"email": "target@example.com", "name": "T", "password": "rightpassword"})

    codes = []
    for _ in range(25):
        response = await client.post(
            "/auth/login",
            data={"username": "target@example.com", "password": "guess"})
        codes.append(response.status_code)

    assert 429 in codes


@pytest.mark.asyncio
async def test_legacy_hash_is_upgraded_on_successful_login(
    client: AsyncClient, db_session
):
    """The old format drains away as users sign in, with no forced reset."""
    import hashlib

    await client.post(
        "/users/",
        json={"email": "legacy@example.com", "name": "L", "password": "oldpassword"})

    # Rewrite the stored hash to the pre-migration format.
    result = await db_session.execute(
        select(User).where(User.email == "legacy@example.com"))
    user = result.scalar_one()
    user.hashed_password = hashlib.sha256(b"oldpassword").hexdigest()
    await db_session.commit()

    response = await client.post(
        "/auth/login",
        data={"username": "legacy@example.com", "password": "oldpassword"})
    assert response.status_code == 200

    await db_session.refresh(user)
    assert user.hashed_password.startswith("$2b$")
    assert verify_password("oldpassword", user.hashed_password)


@pytest.mark.asyncio
async def test_deactivated_user_cannot_log_in(client: AsyncClient):
    user, token = await register_and_login(client, "off@example.com")
    await client.post(
        f"/users/{user['id']}/deactivate",
        headers={"Authorization": f"Bearer {token}"})

    response = await client.post(
        "/auth/login",
        data={"username": "off@example.com",
              "password": "correct horse battery staple"})

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [
    None,
    "",
    "Bearer",
    "Bearer not-a-token",
    "Basic dXNlcjpwYXNz",
    "Bearer eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.",
])
async def test_bad_authorization_headers_are_refused(client: AsyncClient, header):
    headers = {"Authorization": header} if header is not None else {}

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_token_for_deleted_user_is_refused(client: AsyncClient):
    """A valid signature is not enough if the subject no longer exists."""
    user, token = await register_and_login(client, "vanish@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    await client.delete(f"/users/{user['id']}", headers=headers)

    response = await client.get("/auth/me", headers=headers)

    assert response.status_code == 401
