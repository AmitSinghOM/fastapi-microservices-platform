import pytest
from httpx import AsyncClient


@pytest.fixture
async def test_user(client: AsyncClient):
    """Create a test user for item tests."""
    response = await client.post(
        "/users/",
        json={
            "email": "itemowner@example.com",
            "name": "Item Owner",
            "password": "password123"
        }
    )
    return response.json()


@pytest.mark.asyncio
async def test_create_item(client: AsyncClient, test_user):
    """Test item creation."""
    response = await client.post(
        "/items/",
        params={"owner_id": test_user["id"]},
        json={
            "title": "Test Item",
            "description": "A test item",
            "price": 29.99
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test Item"
    assert data["price"] == 29.99
    assert data["owner_id"] == test_user["id"]


@pytest.mark.asyncio
async def test_create_item_negative_price(client: AsyncClient, test_user):
    """Test rejection of negative price."""
    response = await client.post(
        "/items/",
        params={"owner_id": test_user["id"]},
        json={
            "title": "Bad Item",
            "price": -10.00
        }
    )
    assert response.status_code == 400
    assert "negative" in response.json()["detail"]


@pytest.mark.asyncio
async def test_get_items(client: AsyncClient, test_user):
    """Test get all items."""
    # Create items
    for i in range(3):
        await client.post(
            "/items/",
            params={"owner_id": test_user["id"]},
            json={"title": f"Item {i}", "price": 10.00 * (i + 1)}
        )
    
    response = await client.get("/items/")
    assert response.status_code == 200
    assert len(response.json()) >= 3


@pytest.mark.asyncio
async def test_get_items_by_owner(client: AsyncClient, test_user):
    """Test get items by owner."""
    # Create items for test user
    await client.post(
        "/items/",
        params={"owner_id": test_user["id"]},
        json={"title": "Owner Item", "price": 15.00}
    )
    
    response = await client.get(f"/items/owner/{test_user['id']}")
    assert response.status_code == 200
    items = response.json()
    assert all(item["owner_id"] == test_user["id"] for item in items)


@pytest.mark.asyncio
async def test_update_item(client: AsyncClient, test_user):
    """Test item update."""
    # Create item
    create_response = await client.post(
        "/items/",
        params={"owner_id": test_user["id"]},
        json={"title": "Original Title", "price": 20.00}
    )
    item_id = create_response.json()["id"]
    
    # Update item
    response = await client.patch(
        f"/items/{item_id}",
        json={"title": "Updated Title", "price": 25.00}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Updated Title"
    assert response.json()["price"] == 25.00


@pytest.mark.asyncio
async def test_delete_item(client: AsyncClient, test_user):
    """Test item deletion."""
    # Create item
    create_response = await client.post(
        "/items/",
        params={"owner_id": test_user["id"]},
        json={"title": "Delete Me", "price": 5.00}
    )
    item_id = create_response.json()["id"]
    
    # Delete item
    response = await client.delete(f"/items/{item_id}")
    assert response.status_code == 204
    
    # Verify deleted
    get_response = await client.get(f"/items/{item_id}")
    assert get_response.status_code == 404
