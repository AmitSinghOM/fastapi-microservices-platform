"""HTTP request context and bounded single-process rate limiting."""

from __future__ import annotations

import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

RATE_LIMITED_PATHS = ("/auth/login", "/users/")
_HITS: dict[str, tuple[float, int]] = {}
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def reset_rate_limits() -> None:
    """Clear process-local counters, primarily for test isolation."""
    _HITS.clear()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Add correlation, timing, and baseline browser-security headers."""

    async def dispatch(self, request: Request, call_next):
        supplied_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_id
            if _REQUEST_ID_PATTERN.fullmatch(supplied_id)
            else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        started = time.perf_counter()

        response = await call_next(request)
        elapsed = time.perf_counter() - started
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = f"{elapsed:.6f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Bounded fixed-window limiter for unauthenticated endpoints.

    This protects a single process. Multi-replica deployments should enforce
    the same policy at a trusted ingress or in a shared atomic data store.
    """

    def __init__(
        self,
        app,
        requests: int | None = None,
        window_seconds: int | None = None,
        max_entries: int | None = None,
    ):
        super().__init__(app)
        settings = get_settings()
        self.requests = requests or settings.rate_limit_requests
        self.window = window_seconds or settings.rate_limit_window_seconds
        self.max_entries = max_entries or settings.rate_limit_max_entries
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
                            f"Too many requests. Try again in {retry_after}s."
                        ),
                        "details": {},
                    }
                },
            )
        return await call_next(request)

    @staticmethod
    def _is_limited(request: Request) -> bool:
        return (
            request.method == "POST"
            and request.url.path in RATE_LIMITED_PATHS
        )

    def _check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        current = self._hits.get(key)
        if current is None or now - current[0] >= self.window:
            self._make_room(now, key)
            self._hits[key] = (now, 1)
            return True, 0

        window_start, count = current
        if count >= self.requests:
            retry_after = max(1, int(self.window - (now - window_start)))
            return False, retry_after

        self._hits[key] = (window_start, count + 1)
        return True, 0

    def _make_room(self, now: float, incoming_key: str) -> None:
        if incoming_key in self._hits or len(self._hits) < self.max_entries:
            return
        expired = [
            key
            for key, (window_start, _) in self._hits.items()
            if now - window_start >= self.window
        ]
        for key in expired:
            self._hits.pop(key, None)
        if len(self._hits) >= self.max_entries:
            oldest = min(self._hits, key=lambda key: self._hits[key][0])
            self._hits.pop(oldest, None)

    @staticmethod
    def _client_key(request: Request) -> str:
        """Use the ASGI peer; proxy trust must be configured at the server."""
        return request.client.host if request.client else "unknown"
