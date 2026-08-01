import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_item(client: AsyncClient, auth):
    """Item creation. Owner comes from the token, not a parameter."""
    user, headers = auth
    response = await client.post(
        "/items/",
        headers=headers,
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
    assert data["owner_id"] == user["id"]


@pytest.mark.asyncio
async def test_create_item_negative_price(client: AsyncClient, auth):
    """Test rejection of negative price."""
    _, headers = auth
    response = await client.post(
        "/items/",
        headers=headers,
        json={"title": "Bad Item", "price": -10.00}
    )
    assert response.status_code == 400
    assert "negative" in response.text.lower()


@pytest.mark.asyncio
async def test_get_items_returns_only_your_own(client: AsyncClient, auth, other_auth):
    """GET /items/ used to return every item belonging to every user."""
    _, headers = auth
    other_user, other_headers = other_auth

    for i in range(3):
        await client.post(
            "/items/", headers=headers,
            json={"title": f"Mine {i}", "price": 10.00 * (i + 1)})
    await client.post(
        "/items/", headers=other_headers,
        json={"title": "Theirs", "price": 99.00})

    response = await client.get("/items/", headers=headers)

    assert response.status_code == 200
    items = response.json()
    assert len(items) == 3
    assert all(item["owner_id"] != other_user["id"] for item in items)
    assert "Theirs" not in [item["title"] for item in items]


@pytest.mark.asyncio
async def test_update_item(client: AsyncClient, auth):
    """Test item update."""
    _, headers = auth
    created = await client.post(
        "/items/", headers=headers,
        json={"title": "Original Title", "price": 20.00})
    item_id = created.json()["id"]

    response = await client.patch(
        f"/items/{item_id}", headers=headers,
        json={"title": "Updated Title", "price": 25.00})

    assert response.status_code == 200
    assert response.json()["title"] == "Updated Title"
    assert response.json()["price"] == 25.00


@pytest.mark.asyncio
async def test_delete_item(client: AsyncClient, auth):
    """Test item deletion."""
    _, headers = auth
    created = await client.post(
        "/items/", headers=headers,
        json={"title": "Delete Me", "price": 5.00})
    item_id = created.json()["id"]

    response = await client.delete(f"/items/{item_id}", headers=headers)
    assert response.status_code == 204

    get_response = await client.get(f"/items/{item_id}", headers=headers)
    assert get_response.status_code == 404


# =============================================================================
# Authorization regression tests
#
# Ownership used to arrive as an `owner_id` query parameter and the service
# check read `if owner_id and item.owner_id != owner_id`. Omitting the
# parameter skipped the check, so any caller could modify any item.
# =============================================================================

@pytest.mark.asyncio
async def test_cannot_delete_another_users_item(client: AsyncClient, auth, other_auth):
    _, headers = auth
    _, intruder_headers = other_auth
    created = await client.post(
        "/items/", headers=headers,
        json={"title": "Not Yours", "price": 50.00})
    item_id = created.json()["id"]

    response = await client.delete(f"/items/{item_id}", headers=intruder_headers)

    assert response.status_code == 404
    # Still there for the real owner.
    assert (await client.get(f"/items/{item_id}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_cannot_update_another_users_item(client: AsyncClient, auth, other_auth):
    _, headers = auth
    _, intruder_headers = other_auth
    created = await client.post(
        "/items/", headers=headers,
        json={"title": "Original", "price": 50.00})
    item_id = created.json()["id"]

    response = await client.patch(
        f"/items/{item_id}", headers=intruder_headers,
        json={"title": "Hijacked"})

    assert response.status_code == 404
    unchanged = await client.get(f"/items/{item_id}", headers=headers)
    assert unchanged.json()["title"] == "Original"


@pytest.mark.asyncio
async def test_cannot_read_another_users_item(client: AsyncClient, auth, other_auth):
    _, headers = auth
    _, intruder_headers = other_auth
    created = await client.post(
        "/items/", headers=headers,
        json={"title": "Private", "price": 50.00})
    item_id = created.json()["id"]

    response = await client.get(f"/items/{item_id}", headers=intruder_headers)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_owner_id_parameter_is_ignored(client: AsyncClient, auth, other_auth):
    """Supplying owner_id must not let a caller create items as someone else."""
    user, headers = auth
    other_user, _ = other_auth

    response = await client.post(
        "/items/",
        headers=headers,
        params={"owner_id": other_user["id"]},
        json={"title": "Spoof Attempt", "price": 10.00},
    )

    assert response.status_code == 201
    assert response.json()["owner_id"] == user["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("post", "/items/"),
    ("get", "/items/"),
    ("get", "/items/1"),
    ("patch", "/items/1"),
    ("delete", "/items/1"),
])
async def test_all_item_endpoints_require_authentication(
    client: AsyncClient, method, path
):
    request = getattr(client, method)
    kwargs = {"json": {"title": "x", "price": 1.0}} if method in ("post", "patch") else {}

    response = await request(path, **kwargs)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_owner_listing_route_is_gone(client: AsyncClient, auth):
    """/items/owner/{id} let anyone enumerate another user's items."""
    user, headers = auth

    response = await client.get(f"/items/owner/{user['id']}", headers=headers)

    assert response.status_code == 404
