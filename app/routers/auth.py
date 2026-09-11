"""Login endpoint."""

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_active_user
from app.db import get_db
from app.dependencies import get_user_service
from app.exceptions import ForbiddenError, UnauthorizedError
from app.login_throttle import (
    check_login_allowed,
    record_login_failure,
    record_login_success,
)
from app.models import User
from app.schemas.auth import TokenResponse
from app.schemas.user import UserResponse
from app.security import create_access_token
from app.services.user_service import UserService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    service: UserService = Depends(get_user_service),
    db: AsyncSession = Depends(get_db),
):
    """Exchange email and password for an access token.

    A wrong password and an unknown email return the same error. Saying which
    one was wrong would turn this endpoint into a way to enumerate registered
    email addresses.

    Brute-force protection is per-account and database-backed, so the
    failure budget holds across API replicas and restarts. While locked the
    endpoint returns 429 with Retry-After instead of evaluating credentials.
    """
    await check_login_allowed(db, form_data.username)
    user = await service.authenticate(
        email=form_data.username, password=form_data.password
    )
    if user is None:
        await record_login_failure(db, form_data.username)
        raise UnauthorizedError("Incorrect email or password")
    if not user.is_active:
        raise ForbiddenError("Account is deactivated")

    await record_login_success(db, form_data.username)
    return TokenResponse(access_token=create_access_token(subject=user.id))


@router.get("/me", response_model=UserResponse)
async def read_current_user(
    current_user: User = Depends(get_current_active_user),
):
    """The authenticated user's own record."""
    return current_user
