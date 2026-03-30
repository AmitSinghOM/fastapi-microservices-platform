from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.services.base import BaseService
from app.schemas.item import ItemCreate, ItemUpdate
from app.decorators import log_execution, retry
from app.models import Item


class ItemService(BaseService):
    """Item domain service handling all item-related business logic."""
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
    
    @log_execution
    async def get_by_id(self, item_id: int) -> Item | None:
        result = await self.db.execute(select(Item).where(Item.id == item_id))
        return result.scalar_one_or_none()
    
    @log_execution
    async def get_all(self, skip: int = 0, limit: int = 100) -> list[Item]:
        result = await self.db.execute(select(Item).offset(skip).limit(limit))
        return list(result.scalars().all())
    
    @log_execution
    async def get_by_owner(self, owner_id: int, skip: int = 0, limit: int = 100) -> list[Item]:
        result = await self.db.execute(
            select(Item).where(Item.owner_id == owner_id).offset(skip).limit(limit)
        )
        return list(result.scalars().all())
    
    @log_execution
    @retry(max_attempts=3, delay=0.5)
    async def create(self, item_data: ItemCreate, owner_id: int) -> Item:
        # Business rule: validate price
        if item_data.price < 0:
            raise ValueError("Price cannot be negative")
        
        item = Item(
            title=item_data.title,
            description=item_data.description,
            price=item_data.price,
            owner_id=owner_id,
            created_at=datetime.utcnow()
        )
        
        self.db.add(item)
        await self.db.commit()
        await self.db.refresh(item)
        return item
    
    @log_execution
    async def update(self, item_id: int, item_data: ItemUpdate, owner_id: int | None = None) -> Item | None:
        item = await self.get_by_id(item_id)
        if not item:
            return None
        
        # Business rule: only owner can update
        if owner_id and item.owner_id != owner_id:
            raise PermissionError("Not authorized to update this item")
        
        update_data = item_data.model_dump(exclude_unset=True)
        
        # Validate price if being updated
        if "price" in update_data and update_data["price"] < 0:
            raise ValueError("Price cannot be negative")
        
        for field, value in update_data.items():
            setattr(item, field, value)
        
        await self.db.commit()
        await self.db.refresh(item)
        return item
    
    @log_execution
    async def delete(self, item_id: int, owner_id: int | None = None) -> bool:
        item = await self.get_by_id(item_id)
        if not item:
            return False
        
        # Business rule: only owner can delete
        if owner_id and item.owner_id != owner_id:
            raise PermissionError("Not authorized to delete this item")
        
        await self.db.delete(item)
        await self.db.commit()
        return True
