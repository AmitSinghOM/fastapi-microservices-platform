from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.factory import ServiceFactory
from app.services.user_service import UserService
from app.services.item_service import ItemService


async def get_service_factory(db: AsyncSession = Depends(get_db)) -> ServiceFactory:
    """Dependency to get service factory."""
    return ServiceFactory(db=db)


async def get_user_service(
    factory: ServiceFactory = Depends(get_service_factory)
) -> UserService:
    """Dependency to get user service via factory."""
    return factory.get_user_service()


async def get_item_service(
    factory: ServiceFactory = Depends(get_service_factory)
) -> ItemService:
    """Dependency to get item service via factory."""
    return factory.get_item_service()
