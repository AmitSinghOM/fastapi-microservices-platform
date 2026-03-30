from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user_service import UserService
from app.services.item_service import ItemService


class ServiceFactory:
    """Factory for creating service instances.
    
    WHY Factory over inheritance:
    - Loose coupling between services
    - Easy mocking for tests
    - Centralized construction logic
    - Easy to swap implementations
    
    Interview line: "Factory avoids deep inheritance trees 
    and keeps construction logic centralized."
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    def get_user_service(self) -> UserService:
        """Create UserService instance."""
        return UserService(db=self.db)
    
    def get_item_service(self) -> ItemService:
        """Create ItemService instance."""
        return ItemService(db=self.db)


# Convenience functions for dependency injection
def get_service_factory(db: AsyncSession) -> ServiceFactory:
    """Get factory instance for DI."""
    return ServiceFactory(db=db)
