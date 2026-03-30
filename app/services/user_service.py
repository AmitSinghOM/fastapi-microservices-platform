from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import hashlib

from app.services.base import BaseService
from app.schemas.user import UserCreate, UserUpdate
from app.decorators import log_execution, retry
from app.models import User
from app.exceptions import NotFoundError, AlreadyExistsError


class UserService(BaseService):
    """User domain service handling all user-related business logic.
    
    No FastAPI imports here - keeps logic reusable across contexts.
    Uses custom exceptions for explicit error contracts.
    """
    
    def __init__(self, db: AsyncSession):
        super().__init__(db)
    
    @log_execution
    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()
    
    @log_execution
    async def get_by_id_or_raise(self, user_id: int) -> User:
        """Get user by ID or raise NotFoundError."""
        user = await self.get_by_id(user_id)
        if not user:
            raise NotFoundError("User", user_id)
        return user
    
    @log_execution
    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()
    
    @log_execution
    async def get_all(self, skip: int = 0, limit: int = 100) -> list[User]:
        result = await self.db.execute(select(User).offset(skip).limit(limit))
        return list(result.scalars().all())
    
    @log_execution
    @retry(max_attempts=3, delay=0.5)
    async def create(self, user_data: UserCreate) -> User:
        # Business rule: check for existing email
        existing = await self.get_by_email(user_data.email)
        if existing:
            raise AlreadyExistsError("User", "email", user_data.email)
        
        # Hash password (simplified - use proper hashing in production)
        hashed_password = hashlib.sha256(user_data.password.encode()).hexdigest()
        
        user = User(
            email=user_data.email,
            name=user_data.name,
            hashed_password=hashed_password,
            created_at=datetime.utcnow(),
            is_active=True
        )
        
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user
    
    @log_execution
    async def update(self, user_id: int, user_data: UserUpdate) -> User | None:
        user = await self.get_by_id(user_id)
        if not user:
            return None
        
        update_data = user_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(user, field, value)
        
        await self.db.commit()
        await self.db.refresh(user)
        return user
    
    @log_execution
    async def update_or_raise(self, user_id: int, user_data: UserUpdate) -> User:
        """Update user or raise NotFoundError."""
        user = await self.update(user_id, user_data)
        if not user:
            raise NotFoundError("User", user_id)
        return user
    
    @log_execution
    async def delete(self, user_id: int) -> bool:
        user = await self.get_by_id(user_id)
        if not user:
            return False
        
        await self.db.delete(user)
        await self.db.commit()
        return True
    
    @log_execution
    async def delete_or_raise(self, user_id: int) -> None:
        """Delete user or raise NotFoundError."""
        deleted = await self.delete(user_id)
        if not deleted:
            raise NotFoundError("User", user_id)
    
    @log_execution
    async def deactivate(self, user_id: int) -> User | None:
        """Soft delete - deactivate user instead of removing."""
        user = await self.get_by_id(user_id)
        if not user:
            return None
        
        user.is_active = False
        await self.db.commit()
        await self.db.refresh(user)
        return user
    
    @log_execution
    async def deactivate_or_raise(self, user_id: int) -> User:
        """Deactivate user or raise NotFoundError."""
        user = await self.deactivate(user_id)
        if not user:
            raise NotFoundError("User", user_id)
        return user
