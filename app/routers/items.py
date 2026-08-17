"""Item endpoints.

Ownership used to arrive as an ``owner_id`` query parameter, and the service
check read ``if owner_id and item.owner_id != owner_id``. Omitting the
parameter therefore skipped the check entirely, so ``DELETE /items/5`` with no
query string deleted anyone's item. The check was opt-in by the caller.

Ownership now comes from the authenticated user and is always enforced.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.auth import get_current_active_user
from app.dependencies import get_item_service
from app.exceptions import NotFoundError
from app.models import User
from app.schemas.item import ItemCreate, ItemResponse, ItemUpdate
from app.services.item_service import ItemService

router = APIRouter(prefix="/items", tags=["items"])

ItemId = Annotated[int, Path(ge=1)]
Offset = Annotated[int, Query(ge=0)]
PageSize = Annotated[
    int,
    Query(ge=1, le=100),
]


@router.post(
    "/",
    response_model=ItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_item(
    item: ItemCreate,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Create an item owned by the authenticated user."""
    return await service.create(item, owner_id=current_user.id)


@router.get("/", response_model=list[ItemResponse])
async def get_items(
    skip: Offset = 0,
    limit: PageSize = 100,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """List a stable, bounded page of the authenticated user's items."""
    return await service.get_by_owner(current_user.id, skip=skip, limit=limit)


@router.get("/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: ItemId,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Get one item owned by the authenticated user."""
    item = await service.get_by_id(item_id)
    if not item or item.owner_id != current_user.id:
        raise NotFoundError("Item", item_id)
    return item


@router.patch("/{item_id}", response_model=ItemResponse)
async def update_item(
    item_id: ItemId,
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
    item_id: ItemId,
    current_user: User = Depends(get_current_active_user),
    service: ItemService = Depends(get_item_service),
):
    """Delete an item the authenticated user owns."""
    deleted = await service.delete(item_id, owner_id=current_user.id)
    if not deleted:
        raise NotFoundError("Item", item_id)
