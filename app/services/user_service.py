from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.decorators import log_execution
from app.exceptions import AlreadyExistsError, NotFoundError
from app.models import User
from app.schemas.user import UserCreate, UserUpdate
from app.security import hash_password, needs_rehash, verify_password
from app.services.base import BaseService

_TIMING_DECOY_HASH = hash_password("timing-decoy-not-a-real-password")


class UserService(BaseService):
    """Reusable account business logic with explicit transaction handling."""

    def __init__(self, db: AsyncSession):
        super().__init__(db)

    @log_execution
    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @log_execution
    async def get_by_id_or_raise(self, user_id: int) -> User:
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
        statement = select(User).order_by(User.id).offset(skip).limit(limit)
        result = await self.db.execute(statement)
        return list(result.scalars().all())

    @log_execution
    async def create(self, user_data: UserCreate) -> User:
        """Create once and let the unique constraint settle races."""
        if await self.get_by_email(user_data.email):
            raise AlreadyExistsError("User", "email", user_data.email)

        user = User(
            email=user_data.email,
            name=user_data.name,
            hashed_password=hash_password(user_data.password),
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )
        self.db.add(user)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise AlreadyExistsError(
                "User", "email", user_data.email
            ) from exc
        await self.db.refresh(user)
        return user

    @log_execution
    async def authenticate(self, email: str, password: str) -> User | None:
        user = await self.get_by_email(email)
        if user is None:
            verify_password(password, _TIMING_DECOY_HASH)
            return None
        if not verify_password(password, user.hashed_password):
            return None

        if needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(password)
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
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            email = update_data.get("email", user.email)
            raise AlreadyExistsError("User", "email", email) from exc
        await self.db.refresh(user)
        return user

    @log_execution
    async def update_or_raise(self, user_id: int, user_data: UserUpdate) -> User:
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
        if not await self.delete(user_id):
            raise NotFoundError("User", user_id)

    @log_execution
    async def deactivate(self, user_id: int) -> User | None:
        user = await self.get_by_id(user_id)
        if not user:
            return None
        user.is_active = False
        await self.db.commit()
        await self.db.refresh(user)
        return user

    @log_execution
    async def deactivate_or_raise(self, user_id: int) -> User:
        user = await self.deactivate(user_id)
        if not user:
            raise NotFoundError("User", user_id)
        return user
