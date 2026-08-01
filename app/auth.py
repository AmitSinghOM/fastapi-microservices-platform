"""Authentication dependencies.

``app/decorators/auth.py`` defined a ``require_auth`` decorator that read
``current_user`` from kwargs, but nothing ever applied it and nothing ever
supplied a user, so every endpoint was open. The identity of the caller has to
come from somewhere the caller cannot choose, which is what this module does:
verify a signed token, load the user, confirm the account is active.

Ownership decisions elsewhere take the user from here, never from a request
parameter.
"""

from __future__ import annotations

import jwt
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.exceptions import ForbiddenError, UnauthorizedError
from app.models import User
from app.security import decode_access_token

# auto_error=False so a missing header raises our UnauthorizedError with a
# consistent body, rather than FastAPI's default shape.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Resolve the authenticated user, or refuse the request.

    Failures are deliberately indistinguishable to the client: a missing,
    malformed, expired or forged token, and a token naming a user who no longer
    exists, all produce the same 401. Distinguishing them would let a caller
    probe which user ids are real.
    """
    if not token:
        raise UnauthorizedError("Not authenticated")

    try:
        payload = decode_access_token(token)
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid or expired token")

    subject = payload.get("sub")
    if subject is None:
        raise UnauthorizedError("Invalid or expired token")

    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise UnauthorizedError("Invalid or expired token")

    user = await db.get(User, user_id)
    if user is None:
        raise UnauthorizedError("Invalid or expired token")

    return user


async def get_current_active_user(
    user: User = Depends(get_current_user),
) -> User:
    """The authenticated user, refused if the account is deactivated.

    Separate from :func:`get_current_user` so a deactivated account gets 403
    rather than 401: the credentials were valid, the account is not usable.
    """
    if not user.is_active:
        raise ForbiddenError("Account is deactivated")
    return user
