from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.item import ItemCreate, ItemUpdate, ItemResponse
from app.services.item_service import ItemService
from app.dependencies import get_item_service

router = APIRouter(prefix="/items", tags=["items"])


@router.post("/", response_model=ItemResponse, status_code=status.HTTP_201_CREATED)
async def create_item(
    item: ItemCreate,
    owner_id: int,  # In production, get from auth token
    service: ItemService = Depends(get_item_service)
):
    """Create a new item.
    
    Router stays thin - only accepts request, validates input, calls service.
    """
    try:
        return await service.create(item, owner_id=owner_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/", response_model=list[ItemResponse])
async def get_items(
    skip: int = 0,
    limit: int = 100,
    service: ItemService = Depends(get_item_service)
):
    """Get all items with pagination."""
    return await service.get_all(skip=skip, limit=limit)


@router.get("/owner/{owner_id}", response_model=list[ItemResponse])
async def get_items_by_owner(
    owner_id: int,
    skip: int = 0,
    limit: int = 100,
    service: ItemService = Depends(get_item_service)
):
    """Get items by owner."""
    return await service.get_by_owner(owner_id, skip=skip, limit=limit)


@router.get("/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: int,
    service: ItemService = Depends(get_item_service)
):
    """Get item by ID."""
    item = await service.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.patch("/{item_id}", response_model=ItemResponse)
async def update_item(
    item_id: int,
    item_data: ItemUpdate,
    owner_id: int | None = None,  # In production, get from auth token
    service: ItemService = Depends(get_item_service)
):
    """Update item by ID."""
    try:
        item = await service.update(item_id, item_data, owner_id=owner_id)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        return item
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: int,
    owner_id: int | None = None,  # In production, get from auth token
    service: ItemService = Depends(get_item_service)
):
    """Delete item by ID."""
    try:
        deleted = await service.delete(item_id, owner_id=owner_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
