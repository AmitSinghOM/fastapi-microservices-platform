import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_user(client: AsyncClient):
    """Test user creation endpoint."""
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
async def test_create_user_duplicate_email(client: AsyncClient):
    """Test duplicate email rejection with custom exception."""
    user_data = {
        "email": "duplicate@example.com",
        "name": "First User",
        "password": "password123"
    }
    
    # Create first user
    await client.post("/users/", json=user_data)
    
    # Try to create duplicate - now returns structured error
    response = await client.post("/users/", json=user_data)
    assert response.status_code == 409  # Conflict
    error = response.json()["error"]
    assert error["code"] == "ALREADY_EXISTS"
    assert "duplicate@example.com" in error["message"]


@pytest.mark.asyncio
async def test_get_user(client: AsyncClient):
    """Test get user by ID."""
    # Create user first
    create_response = await client.post(
        "/users/",
        json={
            "email": "getuser@example.com",
            "name": "Get User",
            "password": "password123"
        }
    )
    user_id = create_response.json()["id"]
    
    # Get user
    response = await client.get(f"/users/{user_id}")
    assert response.status_code == 200
    assert response.json()["email"] == "getuser@example.com"


@pytest.mark.asyncio
async def test_get_user_not_found(client: AsyncClient):
    """Test 404 for non-existent user with structured error."""
    response = await client.get("/users/99999")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert "99999" in error["message"]


@pytest.mark.asyncio
async def test_update_user(client: AsyncClient):
    """Test user update."""
    # Create user
    create_response = await client.post(
        "/users/",
        json={
            "email": "update@example.com",
            "name": "Original Name",
            "password": "password123"
        }
    )
    user_id = create_response.json()["id"]
    
    # Update user
    response = await client.patch(
        f"/users/{user_id}",
        json={"name": "Updated Name"}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Updated Name"


@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient):
    """Test user deletion."""
    # Create user
    create_response = await client.post(
        "/users/",
        json={
            "email": "delete@example.com",
            "name": "Delete Me",
            "password": "password123"
        }
    )
    user_id = create_response.json()["id"]
    
    # Delete user
    response = await client.delete(f"/users/{user_id}")
    assert response.status_code == 204
    
    # Verify deleted
    get_response = await client.get(f"/users/{user_id}")
    assert get_response.status_code == 404


@pytest.mark.asyncio
async def test_deactivate_user(client: AsyncClient):
    """Test user deactivation (soft delete)."""
    # Create user
    create_response = await client.post(
        "/users/",
        json={
            "email": "deactivate@example.com",
            "name": "Deactivate Me",
            "password": "password123"
        }
    )
    user_id = create_response.json()["id"]
    
    # Deactivate user
    response = await client.post(f"/users/{user_id}/deactivate")
    assert response.status_code == 200
    assert response.json()["is_active"] is False



# =============================================================================
# FAILING TEST EXAMPLES
# These tests are intentionally designed to fail for demonstration purposes.
# =============================================================================

@pytest.mark.asyncio
@pytest.mark.xfail(reason="Intentional failing test - demonstrates expected failure")
async def test_user_password_validation_not_implemented(client: AsyncClient):
    """FAILING TEST: Password strength validation not yet implemented.
    
    This test documents a missing feature - password validation.
    Currently accepts any password, but should enforce:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one number
    
    WHY include failing tests:
    - Documents known gaps/TODOs
    - Prevents regression when feature is added
    - Shows test-driven development approach
    """
    response = await client.post(
        "/users/",
        json={
            "email": "weakpass@example.com",
            "name": "Weak Password User",
            "password": "123"  # Too weak - should fail validation
        }
    )
    # This SHOULD return 422 with validation error, but currently returns 201
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "password" in str(error["details"]).lower()


@pytest.mark.asyncio
@pytest.mark.xfail(reason="Intentional failing test - rate limiting not implemented")
async def test_rate_limiting_not_implemented(client: AsyncClient):
    """FAILING TEST: Rate limiting not yet implemented.
    
    Documents that the API should have rate limiting but doesn't.
    """
    # Make 100 rapid requests - should be rate limited
    for _ in range(100):
        await client.get("/users/")
    
    response = await client.get("/users/")
    # Should return 429 Too Many Requests
    assert response.status_code == 429
