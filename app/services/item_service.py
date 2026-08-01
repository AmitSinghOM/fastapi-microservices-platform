from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.services.base import BaseService
from app.schemas.item import ItemCreate, ItemUpdate
from app.decorators import log_execution, retry
from app.exceptions import ValidationError
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
            raise ValidationError("Price cannot be negative", field="price")
        
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
    async def update(self, item_id: int, item_data: ItemUpdate, owner_id: int) -> Item | None:
        """Update an item. ``owner_id`` is required and always enforced.

        It used to default to None and the check was `if owner_id and ...`, so
        a caller that omitted it bypassed authorization entirely. There is no
        longer a way to call this without an owner.
        """
        item = await self.get_by_id(item_id)
        if not item:
            return None
        
        # Business rule: only the owner can update. Reported as "not found" by
        # the router so a foreign id is not confirmed to exist.
        if item.owner_id != owner_id:
            return None
        
        update_data = item_data.model_dump(exclude_unset=True)
        
        # Validate price if being updated
        if "price" in update_data and update_data["price"] < 0:
            raise ValidationError("Price cannot be negative", field="price")
        
        for field, value in update_data.items():
            setattr(item, field, value)
        
        await self.db.commit()
        await self.db.refresh(item)
        return item
    
    @log_execution
    async def delete(self, item_id: int, owner_id: int) -> bool:
        """Delete an item. ``owner_id`` is required and always enforced."""
        item = await self.get_by_id(item_id)
        if not item:
            return False
        
        # Business rule: only the owner can delete.
        if item.owner_id != owner_id:
            return False
        
        await self.db.delete(item)
        await self.db.commit()
        return True
