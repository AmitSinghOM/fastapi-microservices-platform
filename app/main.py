from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import get_settings
from app.db import close_database, database_is_ready, init_db
from app.exception_handlers import register_exception_handlers
from app.middleware import RateLimitMiddleware, RequestContextMiddleware
from app.routers import (
    auth_router,
    items_router,
    users_router,
    webhooks_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize local resources and always close database connections."""
    del app
    settings = get_settings()
    if settings.auto_create_schema:
        await init_db()
    try:
        yield
    finally:
        await close_database()


def create_app() -> FastAPI:
    """Build a fully configured FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        summary="Async webhook platform with authenticated management",
        description=(
            "A production-oriented FastAPI webhook platform with async "
            "SQLAlchemy, OAuth2 bearer management authentication, project "
            "API keys, durable delivery, health probes, request tracing, "
            "and consistent errors."
        ),
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "auth", "description": "Authentication and identity"},
            {"name": "users", "description": "Account lifecycle"},
            {"name": "items", "description": "Owned item CRUD operations"},
            {"name": "webhooks", "description": "Webhook control and ingestion"},
            {"name": "health", "description": "Orchestrator health probes"},
        ],
    )

    register_exception_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=1_000)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-API-Key",
                "X-Request-ID",
            ],
            expose_headers=["X-Request-ID", "X-Process-Time"],
        )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts,
    )
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(items_router)
    app.include_router(webhooks_router)

    @app.get("/", include_in_schema=False)
    async def service_index() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs" if settings.docs_enabled else "disabled",
            "health": "/readyz",
        }

    @app.get("/livez", tags=["health"], summary="Liveness probe")
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz", tags=["health"], summary="Readiness probe")
    async def readiness():
        if await database_is_ready():
            return {"status": "ready"}
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready"},
        )

    @app.get(
        "/health",
        tags=["health"],
        deprecated=True,
        summary="Compatibility liveness probe",
    )
    async def health_check() -> dict[str, str]:
        return {"status": "healthy"}

    return app


app = create_app()
