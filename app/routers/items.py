"""Item endpoints.

Ownership used to arrive as an ``owner_id`` query parameter, and the service
check read ``if owner_id and item.owner_id != owner_id``. Omitting the
parameter therefore skipped the check entirely, so ``DELETE /items/5`` with no
query string deleted anyone's item. The check was opt-in by the caller.

Ownership now comes from the authenticated user and is always enforced.
"""

from fastapi import APIRouter, Depends, status

from app.auth import get_current_active_user
from app.dependencies import get_item_service
from app.exceptions import NotFoundError
from app.models import User
from app.schemas.item import ItemCreate, ItemResponse, ItemUpdate
from app.services.item_service import ItemService

router = APIRouter(prefix="/items", tags=["items"])


@router.post("/", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
async def create_item(
    item: ItemCreate,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Create an item owned by the authenticated user."""
    return await service.create(item, owner_id=current_user.id)


@router.get("/", response_model=list[ItemResponse])
async def get_items(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """List the authenticated user's items.

    This used to return every item belonging to every user.
    """
    return await service.get_by_owner(current_user.id, skip=skip, limit=limit)


@router.get("/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: int,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Get one of the authenticated user's items.

    Another user's item reports 404 rather than 403: a 403 would confirm the id
    exists, which is enough to enumerate the table.
    """
    item = await service.get_by_id(item_id)
    if not item or item.owner_id != current_user.id:
        raise NotFoundError("Item", item_id)
    return item


@router.patch("/{item_id}", response_model=ItemResponse)
async def update_item(
    item_id: int,
    item_data: ItemUpdate,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Update an item the authenticated user owns."""
    item = await service.update(item_id, item_data, owner_id=current_user.id)
    if not item:
        raise NotFoundError("Item", item_id)
    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: int,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Delete an item the authenticated user owns."""
    deleted = await service.delete(item_id, owner_id=current_user.id)
    if not deleted:
        raise NotFoundError("Item", item_id)
