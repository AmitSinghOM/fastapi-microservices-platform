"""Custom exceptions for explicit error handling.

WHY custom exceptions:
- Explicit error contracts between layers
- Services throw domain exceptions, routers translate to HTTP
- Easy to add context and error codes
- Centralized error handling via global handler
"""


class AppException(Exception):
    """Base exception for all application errors."""
    
    def __init__(self, message: str, code: str = "APP_ERROR", details: dict = None):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(AppException):
    """Resource not found."""
    
    def __init__(self, resource: str, identifier: str | int):
        super().__init__(
            message=f"{resource} with id '{identifier}' not found",
            code="NOT_FOUND",
            details={"resource": resource, "identifier": str(identifier)}
        )


class AlreadyExistsError(AppException):
    """Resource already exists."""
    
    def __init__(self, resource: str, field: str, value: str):
        super().__init__(
            message=f"{resource} with {field} '{value}' already exists",
            code="ALREADY_EXISTS",
            details={"resource": resource, "field": field, "value": value}
        )


class ValidationError(AppException):
    """Business validation failed."""
    
    def __init__(self, message: str, field: str = None):
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            details={"field": field} if field else {}
        )


class UnauthorizedError(AppException):
    """Authentication required or failed."""
    
    def __init__(self, message: str = "Authentication required"):
        super().__init__(message=message, code="UNAUTHORIZED")


class ForbiddenError(AppException):
    """Permission denied."""
    
    def __init__(self, message: str = "Permission denied"):
        super().__init__(message=message, code="FORBIDDEN")


class DatabaseError(AppException):
    """Database operation failed."""
    
    def __init__(self, operation: str, message: str = "Database operation failed"):
        super().__init__(
            message=message,
            code="DATABASE_ERROR",
            details={"operation": operation}
        )
