import pytest
from httpx import AsyncClient

from app.tests.conftest import register_and_login


@pytest.mark.asyncio
async def test_create_user(client: AsyncClient):
    """Registration stays open — it has to be."""
    response = await client.post(
        "/users/",
        json={
            "email": "test@example.com",
            "name": "Test User",
            "password": "securepassword123"
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["name"] == "Test User"
    assert "id" in data
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_create_user_never_returns_password_material(client: AsyncClient):
    response = await client.post(
        "/users/",
        json={"email": "leak@example.com", "name": "N", "password": "hunter2hunter2"},
    )

    body = response.text.lower()
    assert "hunter2" not in body
    assert "password" not in response.json()


@pytest.mark.asyncio
async def test_create_user_duplicate_email(client: AsyncClient):
    """Test duplicate email rejection with custom exception."""
    user_data = {
        "email": "duplicate@example.com",
        "name": "First User",
        "password": "password123"
    }
    
    await client.post("/users/", json=user_data)
    
    response = await client.post("/users/", json=user_data)
    assert response.status_code == 409  # Conflict
    error = response.json()["error"]
    assert error["code"] == "ALREADY_EXISTS"
    assert "duplicate@example.com" in error["message"]


@pytest.mark.asyncio
async def test_get_own_user(client: AsyncClient, auth):
    user, headers = auth

    response = await client.get("/users/me", headers=headers)

    assert response.status_code == 200
    assert response.json()["email"] == user["email"]


@pytest.mark.asyncio
async def test_get_user_by_own_id(client: AsyncClient, auth):
    user, headers = auth

    response = await client.get(f"/users/{user['id']}", headers=headers)

    assert response.status_code == 200
    assert response.json()["email"] == user["email"]


@pytest.mark.asyncio
async def test_get_user_not_found(client: AsyncClient, auth):
    """A non-existent id reports not found."""
    _, headers = auth

    response = await client.get("/users/99999", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_update_user(client: AsyncClient, auth):
    user, headers = auth

    response = await client.patch(
        f"/users/{user['id']}", headers=headers,
        json={"name": "Updated Name"})

    assert response.status_code == 200
    assert response.json()["name"] == "Updated Name"


@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient, auth):
    user, headers = auth

    response = await client.delete(f"/users/{user['id']}", headers=headers)
    assert response.status_code == 204

    # Token now refers to a user who no longer exists.
    get_response = await client.get("/users/me", headers=headers)
    assert get_response.status_code == 401


@pytest.mark.asyncio
async def test_deactivate_user(client: AsyncClient, auth):
    user, headers = auth

    response = await client.post(
        f"/users/{user['id']}/deactivate", headers=headers)

    assert response.status_code == 200
    assert response.json()["is_active"] is False


# =============================================================================
# Authorization regression tests
#
# Every route below used to be reachable with no credentials at all:
# GET /users/ returned every registered email, and DELETE /users/{id} removed
# any account.
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("get", "/users/me"),
    ("get", "/users/1"),
    ("patch", "/users/1"),
    ("delete", "/users/1"),
    ("post", "/users/1/deactivate"),
])
async def test_user_endpoints_require_authentication(client: AsyncClient, method, path):
    request = getattr(client, method)
    kwargs = {"json": {"name": "x"}} if method == "patch" else {}

    response = await request(path, **kwargs)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bulk_user_listing_is_gone(client: AsyncClient, auth):
    """GET /users/ exposed every registered email address."""
    _, headers = auth

    response = await client.get("/users/", headers=headers)

    assert response.status_code == 405


@pytest.mark.asyncio
async def test_cannot_read_another_users_record(client: AsyncClient, auth, other_auth):
    _, headers = auth
    victim, _ = other_auth

    response = await client.get(f"/users/{victim['id']}", headers=headers)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cannot_delete_another_users_account(client: AsyncClient, auth, other_auth):
    _, headers = auth
    victim, victim_headers = other_auth

    response = await client.delete(f"/users/{victim['id']}", headers=headers)

    assert response.status_code == 404
    # Victim's account survives.
    assert (await client.get("/users/me", headers=victim_headers)).status_code == 200


@pytest.mark.asyncio
async def test_cannot_deactivate_another_users_account(
    client: AsyncClient, auth, other_auth
):
    _, headers = auth
    victim, victim_headers = other_auth

    response = await client.post(
        f"/users/{victim['id']}/deactivate", headers=headers)

    assert response.status_code == 404
    still_active = await client.get("/users/me", headers=victim_headers)
    assert still_active.json()["is_active"] is True


@pytest.mark.asyncio
async def test_deactivated_account_cannot_use_its_token(client: AsyncClient):
    user, token = await register_and_login(client, "gone@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    await client.post(f"/users/{user['id']}/deactivate", headers=headers)

    response = await client.get("/users/me", headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_registration_is_rate_limited(client: AsyncClient):
    """Was unbounded: an attacker could create accounts without limit."""
    codes = []
    for i in range(25):
        response = await client.post(
            "/users/",
            json={"email": f"flood{i}@example.com", "name": "F",
                  "password": "password123"},
        )
        codes.append(response.status_code)

    assert 429 in codes
    limited = await client.post(
        "/users/",
        json={"email": "final@example.com", "name": "F", "password": "password123"},
    )
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
