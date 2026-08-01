"""Rate limiting.

Registration and login had no limit, so both were free to hammer: unlimited
password guesses against /auth/login, and unlimited account creation.

This is a fixed-window in-memory limiter. That is honest about its scope — it
counts per process, so N replicas allow N times the limit, and the counters
reset on restart. It raises the cost of scripted guessing on a single-instance
deployment. A multi-instance deployment needs a shared counter in Redis, or
limiting at the ingress.
"""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

# Endpoints reachable without credentials, so the only thing standing between
# an attacker and unlimited attempts.
RATE_LIMITED_PATHS = (
    "/auth/login",
    "/users/",
)


# Counters live at module scope so they can be inspected and cleared, which
# tests need in order to stay independent of each other.
_HITS: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))


def reset_rate_limits() -> None:
    """Clear all rate-limit counters."""
    _HITS.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-client limit on unauthenticated endpoints."""

    def __init__(self, app, requests: int | None = None,
                 window_seconds: int | None = None):
        super().__init__(app)
        settings = get_settings()
        self.requests = requests or settings.rate_limit_requests
        self.window = window_seconds or settings.rate_limit_window_seconds
        self._hits = _HITS

    async def dispatch(self, request: Request, call_next):
        if not self._is_limited(request):
            return await call_next(request)

        key = f"{self._client_key(request)}:{request.url.path}"
        allowed, retry_after = self._check(key)
        if not allowed:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": (
                            f"Too many requests. Try again in "
                            f"{retry_after}s."),
                        "details": {},
                    }
                },
            )
        return await call_next(request)

    def _is_limited(self, request: Request) -> bool:
        if request.method == "POST" and request.url.path in RATE_LIMITED_PATHS:
            return True
        return False

    def _check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        window_start, count = self._hits[key]

        if now - window_start >= self.window:
            self._hits[key] = (now, 1)
            return True, 0

        if count >= self.requests:
            return False, max(1, int(self.window - (now - window_start)))

        self._hits[key] = (window_start, count + 1)
        return True, 0

    @staticmethod
    def _client_key(request: Request) -> str:
        """Identify the client.

        X-Forwarded-For is deliberately ignored: it is caller-supplied, so
        trusting it lets an attacker rotate the header and bypass the limit
        entirely. Behind a proxy, configure uvicorn's --forwarded-allow-ips so
        request.client reflects the real peer.
        """
        return request.client.host if request.client else "unknown"
