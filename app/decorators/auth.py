import functools
from typing import Callable


class AuthorizationError(Exception):
    """Raised when authorization fails."""
    pass


def require_auth(roles: list[str] | None = None) -> Callable:
    """Decorator to enforce authorization on service methods.
    
    In production, this would integrate with your auth system.
    Here it demonstrates the pattern for cross-cutting auth concerns.
    """
    
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Extract user context from kwargs or service instance
            user = kwargs.get("current_user")
            
            if user is None:
                raise AuthorizationError("Authentication required")
            
            if roles:
                user_roles = getattr(user, "roles", [])
                if not any(role in user_roles for role in roles):
                    raise AuthorizationError(f"Required roles: {roles}")
            
            return await func(*args, **kwargs)
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            user = kwargs.get("current_user")
            
            if user is None:
                raise AuthorizationError("Authentication required")
            
            if roles:
                user_roles = getattr(user, "roles", [])
                if not any(role in user_roles for role in roles):
                    raise AuthorizationError(f"Required roles: {roles}")
            
            return func(*args, **kwargs)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator
