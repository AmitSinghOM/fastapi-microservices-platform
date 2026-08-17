from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.decorators import log_execution
from app.exceptions import ValidationError
from app.models import Item
from app.schemas.item import ItemCreate, ItemUpdate
from app.services.base import BaseService


class ItemService(BaseService):
    """Item domain service handling ownership-aware CRUD operations."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    @log_execution
    async def get_by_id(self, item_id: int) -> Item | None:
        result = await self.db.execute(select(Item).where(Item.id == item_id))
        return result.scalar_one_or_none()

    @log_execution
    async def get_all(self, skip: int = 0, limit: int = 100) -> list[Item]:
        statement = select(Item).order_by(Item.id).offset(skip).limit(limit)
        result = await self.db.execute(statement)
        return list(result.scalars().all())

    @log_execution
    async def get_by_owner(
        self,
        owner_id: int,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Item]:
        statement = (
            select(Item)
            .where(Item.owner_id == owner_id)
            .order_by(Item.id)
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(statement)
        return list(result.scalars().all())

    @log_execution
    async def create(self, item_data: ItemCreate, owner_id: int) -> Item:
        """Create once; non-idempotent writes are deliberately not retried."""
        if item_data.price < 0:
            raise ValidationError("Price cannot be negative", field="price")

        item = Item(
            title=item_data.title,
            description=item_data.description,
            price=item_data.price,
            owner_id=owner_id,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(item)
        await self.db.commit()
        await self.db.refresh(item)
        return item

    @log_execution
    async def update(
        self,
        item_id: int,
        item_data: ItemUpdate,
        owner_id: int,
    ) -> Item | None:
        item = await self.get_by_id(item_id)
        if not item or item.owner_id != owner_id:
            return None

        update_data = item_data.model_dump(exclude_unset=True)
        if "price" in update_data and update_data["price"] < 0:
            raise ValidationError("Price cannot be negative", field="price")
        for field, value in update_data.items():
            setattr(item, field, value)

        await self.db.commit()
        await self.db.refresh(item)
        return item

    @log_execution
    async def delete(self, item_id: int, owner_id: int) -> bool:
        item = await self.get_by_id(item_id)
        if not item or item.owner_id != owner_id:
            return False

        await self.db.delete(item)
        await self.db.commit()
        return True
