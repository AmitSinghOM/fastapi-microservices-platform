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

## Authentication

All endpoints require a bearer token except registration (`POST /users/`) and
`GET /health`.

```bash
# 1. Register
curl -X POST localhost:8000/users/ -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","name":"A","password":"a-good-long-password"}'

# 2. Log in (form-encoded, OAuth2 password flow)
curl -X POST localhost:8000/auth/login \
  -d 'username=a@b.com&password=a-good-long-password'
# -> {"access_token":"...","token_type":"bearer"}

# 3. Call an endpoint
curl localhost:8000/items/ -H 'Authorization: Bearer <token>'
```

`SECRET_KEY` must be set in any real deployment. Left unset, the app generates
a random key per process and warns on startup, so tokens stop working after a
restart:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Authorization model

A caller may only read or modify their own records. Ownership is taken from the
verified token, never from a request parameter — an `owner_id` in the query
string is ignored.

Accessing another user's record returns `404`, not `403`. A `403` would confirm
the id exists, which is enough to enumerate the table.

### Passwords

bcrypt with a cost of 12, salted per hash. Accounts created before this change
used unsalted SHA-256; those hashes still verify, and each one is transparently
upgraded to bcrypt the first time its owner logs in, so no password reset is
needed.

### Rate limiting

`POST /auth/login` and `POST /users/` are limited per client IP
(`RATE_LIMIT_REQUESTS` per `RATE_LIMIT_WINDOW_SECONDS`). The counter is
in-process, so N replicas allow N times the limit — a multi-instance deployment
should limit at the ingress or move the counter to Redis.

`X-Forwarded-For` is deliberately ignored, since a caller can set it freely and
defeat the limit. Behind a trusted proxy, run uvicorn with
`--forwarded-allow-ips` so `request.client` reflects the real peer.

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
