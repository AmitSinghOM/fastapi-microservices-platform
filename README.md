# FastAPI Microservices Platform

Production-ready FastAPI backend demonstrating scalable architecture patterns.

## Architecture

```
Client
  │
  ▼
FastAPI App
  ├── Routers (API layer - thin, delegates to services)
  ├── Services (Business logic - reusable, testable)
  ├── Decorators (Cross-cutting: logging, retry, auth)
  ├── Factory (Service creation - loose coupling)
  ├── Schemas (Pydantic validation - strict contracts)
  └── Tests (Unit + Integration)
```

## Design Decisions

### 1. Thin Routers
Routers only accept requests, validate input, and call services. No business logic.

### 2. Service Layer
- One service per domain (User, Item)
- No FastAPI imports - reusable in workers, cron jobs, async consumers
- Decorated with logging and retry logic

### 3. Factory Pattern (not inheritance)
- Loose coupling between services
- Easy mocking for tests
- Centralized construction logic

### 4. Decorator Pattern
Cross-cutting concerns without polluting business logic:
- `@log_execution` - timing and logging
- `@retry` - exponential backoff
- `@require_auth` - authorization checks

### 5. Async-First
- Async endpoints and DB operations
- Improves throughput for IO-bound work
- CPU-heavy tasks should move to workers

### 6. Pydantic Schemas
Strict contracts between frontend and backend with auto-validation.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
uvicorn app.main:app --reload

# Run with Docker
docker-compose up --build
```

## Testing

```bash
# Run all tests
pytest app/tests/ -v

# Run with coverage
pytest app/tests/ --cov=app
```

## API Docs

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Project Structure

```
app/
├── main.py           # App factory
├── config.py         # Settings
├── db.py             # Database setup
├── models.py         # SQLAlchemy models
├── dependencies.py   # FastAPI DI
├── routers/          # API endpoints
├── services/         # Business logic
├── decorators/       # Cross-cutting concerns
├── schemas/          # Pydantic models
└── tests/            # Test suite
```

## Interview Talking Points

> "Routers stay thin; services own logic."

> "Factory avoids deep inheritance trees and keeps construction logic centralized."

> "Decorator pattern keeps core logic clean and makes behavior composable."

> "Services have no FastAPI imports - I can reuse logic in workers, cron jobs, or async consumers."

> "Async improves throughput but CPU tasks must move to workers."
