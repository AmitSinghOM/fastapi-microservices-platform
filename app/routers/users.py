"""User endpoints.

Every route here used to be unauthenticated: GET /users/ returned every user,
and DELETE /users/{id} removed any account. A require_auth decorator existed but
was applied nowhere.

Registration stays open because it has to. Everything else requires a token,
and a user may only read or modify their own record.
"""

from fastapi import APIRouter, Depends, status

from app.auth import get_current_active_user
from app.dependencies import get_user_service
from app.exceptions import NotFoundError
from app.models import User
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


def _own_record_or_404(user_id: int, current_user: User) -> None:
    """Refuse access to another user's record.

    Reported as "not found" rather than "forbidden" so a caller cannot use the
    difference to discover which user ids exist.
    """
    if user_id != current_user.id:
        raise NotFoundError("User", user_id)


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    user: UserCreate,
    service: UserService = Depends(get_user_service)
):
    """Register a new user.
    
    Intentionally unauthenticated. Rate limited in middleware, since otherwise
    this is an unbounded account-creation endpoint.
    """
    return await service.create(user)


@router.get("/me", response_model=UserResponse)
async def get_own_user(
    current_user: User = Depends(get_current_active_user),
):
    """The authenticated user's own record.
    
    Replaces the unauthenticated GET /users/ listing, which exposed every
    registered email address.
    """
    return current_user


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    current_user: User = Depends(get_current_active_user),
    service: UserService = Depends(get_user_service)
):
    """Get a user by ID. Only your own record."""
    _own_record_or_404(user_id, current_user)
    return await service.get_by_id_or_raise(user_id)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    service: UserService = Depends(get_user_service)
):
    """Update a user by ID. Only your own record."""
    _own_record_or_404(user_id, current_user)
    return await service.update_or_raise(user_id, user_data)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_active_user),
    service: UserService = Depends(get_user_service)
):
    """Delete a user by ID. Only your own record."""
    _own_record_or_404(user_id, current_user)
    await service.delete_or_raise(user_id)


@router.post("/{user_id}/deactivate", response_model=UserResponse)
async def deactivate_user(
    user_id: int,
    current_user: User = Depends(get_current_active_user),
    service: UserService = Depends(get_user_service)
):
    """Soft delete - deactivate your own account."""
    _own_record_or_404(user_id, current_user)
    return await service.deactivate_or_raise(user_id)
