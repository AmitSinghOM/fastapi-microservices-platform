"""Standardized API exception handling and safe server-side logging."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.exceptions import (
    AlreadyExistsError,
    AppException,
    DatabaseError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)

logger = logging.getLogger(__name__)


def create_error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Create a standardized, non-leaky error response."""
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


async def app_exception_handler(
    request: Request,
    exc: AppException,
) -> JSONResponse:
    """Map domain exceptions to stable HTTP error contracts."""
    del request
    status_map = {
        NotFoundError: status.HTTP_404_NOT_FOUND,
        AlreadyExistsError: status.HTTP_409_CONFLICT,
        ValidationError: status.HTTP_400_BAD_REQUEST,
        UnauthorizedError: status.HTTP_401_UNAUTHORIZED,
        ForbiddenError: status.HTTP_403_FORBIDDEN,
        DatabaseError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    status_code = status_map.get(
        type(exc),
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )

    log = logger.error if status_code >= 500 else logger.warning
    log("Application error %s: %s", exc.code, exc.message)
    headers = (
        {"WWW-Authenticate": "Bearer"}
        if isinstance(exc, UnauthorizedError)
        else None
    )
    return create_error_response(
        status_code=status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Return field-focused Pydantic validation failures."""
    del request
    errors = [
        {
            "field": ".".join(str(location) for location in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    logger.warning("Request validation failed with %d errors", len(errors))
    return create_error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details={"errors": errors},
    )


async def sqlalchemy_exception_handler(
    request: Request,
    exc: SQLAlchemyError,
) -> JSONResponse:
    """Hide database internals while retaining a server-side traceback."""
    del request
    logger.error("Database operation failed", exc_info=exc)
    return create_error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="DATABASE_ERROR",
        message="A database error occurred",
    )


async def generic_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Hide unexpected implementation details from API clients."""
    del request
    logger.error("Unexpected application error", exc_info=exc)
    return create_error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="An unexpected error occurred",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers."""
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,
    )
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
