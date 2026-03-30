"""Global exception handlers for FastAPI.

WHY global handlers:
- Centralized error response formatting
- Consistent error structure across all endpoints
- Logging and monitoring in one place
- Clean separation from business logic
"""

import logging
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.exceptions import (
    AppException,
    NotFoundError,
    AlreadyExistsError,
    ValidationError,
    UnauthorizedError,
    ForbiddenError,
    DatabaseError,
)

logger = logging.getLogger(__name__)


def create_error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict = None
) -> JSONResponse:
    """Create standardized error response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {}
            }
        }
    )


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """Handle custom application exceptions."""
    
    # Map exception types to HTTP status codes
    status_map = {
        NotFoundError: status.HTTP_404_NOT_FOUND,
        AlreadyExistsError: status.HTTP_409_CONFLICT,
        ValidationError: status.HTTP_400_BAD_REQUEST,
        UnauthorizedError: status.HTTP_401_UNAUTHORIZED,
        ForbiddenError: status.HTTP_403_FORBIDDEN,
        DatabaseError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    
    status_code = status_map.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    logger.error(f"AppException: {exc.code} - {exc.message}", extra={"details": exc.details})
    
    return create_error_response(
        status_code=status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details
    )


async def validation_exception_handler(
    request: Request, 
    exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors."""
    
    errors = []
    for error in exc.errors():
        errors.append({
            "field": ".".join(str(loc) for loc in error["loc"]),
            "message": error["msg"],
            "type": error["type"]
        })
    
    logger.warning(f"Validation error: {errors}")
    
    return create_error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        details={"errors": errors}
    )


async def sqlalchemy_exception_handler(
    request: Request, 
    exc: SQLAlchemyError
) -> JSONResponse:
    """Handle database errors."""
    
    logger.error(f"Database error: {exc}", exc_info=True)
    
    return create_error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="DATABASE_ERROR",
        message="A database error occurred",
        details={}  # Don't expose internal DB errors
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for unexpected errors."""
    
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    
    return create_error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="An unexpected error occurred",
        details={}
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers with the app."""
    
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
