from abc import ABC, abstractmethod
from sqlalchemy.ext.asyncio import AsyncSession


class BaseService(ABC):
    """Base service class providing common functionality.
    
    Services own business logic and are independent of HTTP layer.
    This allows reuse in workers, cron jobs, or async consumers.
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    @abstractmethod
    async def get_by_id(self, id: int):
        """Retrieve entity by ID."""
        pass
    
    @abstractmethod
    async def get_all(self, skip: int = 0, limit: int = 100):
        """Retrieve all entities with pagination."""
        pass
    
    @abstractmethod
    async def create(self, data):
        """Create new entity."""
        pass
    
    @abstractmethod
    async def update(self, id: int, data):
        """Update existing entity."""
        pass
    
    @abstractmethod
    async def delete(self, id: int):
        """Delete entity by ID."""
        pass
