from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.routers import users_router, items_router
from app.exception_handlers import register_exception_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown events."""
    # Startup
    await init_db()
    yield
    # Shutdown (cleanup if needed)


def create_app() -> FastAPI:
    """Application factory pattern."""
    settings = get_settings()
    
    app = FastAPI(
        title=settings.app_name,
        description="Production-ready FastAPI microservices platform",
        version="1.0.0",
        lifespan=lifespan
    )
    
    # Register global exception handlers
    register_exception_handlers(app)
    
    # Register routers
    app.include_router(users_router)
    app.include_router(items_router)
    
    @app.get("/health")
    async def health_check():
        return {"status": "healthy"}
    
    return app


app = create_app()
