# FastAPI Service Platform

An async, production-oriented FastAPI reference service with a clear router →
service → persistence architecture. It is one deployable service today, not a
distributed microservice system; the boundaries are designed so domains can be
split later when operational needs justify it.

## Included capabilities

- Async FastAPI endpoints and SQLAlchemy 2 sessions
- OAuth2 password login with short-lived signed JWT bearer tokens
- bcrypt password hashing and transparent migration of legacy SHA-256 hashes
- Per-user authorization for account and item operations
- Strict Pydantic v2 request/response contracts and standardized error bodies
- Fixed-precision item prices, relational checks, and cascading ownership
- Stable offset pagination (`skip >= 0`, `1 <= limit <= 100`)
- Request IDs, process timing, gzip, trusted-host validation, optional CORS,
  and baseline browser-security headers
- Bounded login/registration rate limiting for a single process
- Liveness (`/livez`), database readiness (`/readyz`), and compatibility
  health (`/health`) probes
- Swagger UI, ReDoc, and OpenAPI metadata, optionally disabled in deployment
- Graceful database pool shutdown and persistent Docker Compose data
- Consistent 401 `WWW-Authenticate: Bearer` challenges

## Architecture

```text
Client
  └─ FastAPI application
      ├─ middleware      request context, hosts, CORS, gzip, rate limiting
      ├─ routers         HTTP contracts and dependency injection
      ├─ services        reusable business and transaction logic
      ├─ schemas         Pydantic validation and serialization
      └─ persistence     async SQLAlchemy models and sessions
```

Routers stay thin, services do not import FastAPI, and request-scoped sessions
are supplied through dependencies. Non-idempotent database writes are not
retried automatically: replaying an ambiguous commit can create duplicate
state. Retry only transient failures at an idempotent boundary with backoff,
jitter, and retry-storm protection.

## Quick start

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open:

- API index: <http://localhost:8000/>
- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>
- OpenAPI JSON: <http://localhost:8000/openapi.json>

## Authentication example

```bash
curl -X POST http://localhost:8000/users/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"user@example.com","name":"Example","password":"long-password"}'

curl -X POST http://localhost:8000/auth/login \
  -d 'username=user@example.com&password=long-password'

curl http://localhost:8000/items/ \
  -H 'Authorization: Bearer <access-token>'
```

Registration and login intentionally return indistinguishable authentication
failures where applicable. Resource ownership always comes from the verified
token, never from a caller-provided owner ID.

## Configuration

Copy `.env.example` and configure environment variables. Important settings:

| Variable | Purpose |
|---|---|
| `ENVIRONMENT` | `development`, `test`, `staging`, or `production` |
| `SECRET_KEY` | JWT signing key; required in staging/production, minimum 32 characters |
| `DATABASE_URL` | SQLAlchemy async database URL |
| `AUTO_CREATE_SCHEMA` | Local convenience; must be `false` when deployed |
| `ALLOWED_HOSTS` | JSON array accepted by trusted-host middleware |
| `CORS_ORIGINS` | JSON array of explicit browser origins; empty disables CORS |
| `DOCS_ENABLED` | Enables Swagger, ReDoc, and OpenAPI endpoints |
| `RATE_LIMIT_*` | Single-process unauthenticated endpoint limits |

Staging/production settings fail closed when the secret is absent, debug is on
in production, schema auto-creation is enabled, or allowed hosts are empty.
Use versioned migrations before deployment; `create_all` is retained only for
local development and tests.

## Docker Compose

```bash
# Set a stable key so issued tokens survive container restarts.
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build
```

The Compose setup stores SQLite data in the named `api-data` volume instead of
inside the replaceable container. SQLite is suitable for this local setup; use
a managed production database and a compatible async driver for multi-instance
deployments.

## Health and operations

- `/livez`: process liveness; does not touch dependencies
- `/readyz`: bounded database connectivity probe; returns 503 when unavailable
- `/health`: deprecated compatibility liveness endpoint
- `X-Request-ID`: accepted when safe, otherwise generated; echoed in responses
- `X-Process-Time`: application processing time in seconds

The built-in rate limiter is deliberately bounded but process-local. Multiple
workers or replicas must use an atomic shared limiter or enforce the policy at
a trusted ingress. Configure proxy trust at the ASGI server; arbitrary
`X-Forwarded-For` values are not trusted by application code.

## Testing

```bash
pytest app/tests -q
python -m compileall -q app
```

The current suite covers authentication, token handling, account isolation,
item ownership, CRUD operations, and rate limiting.

## Project structure

```text
app/
├── main.py               application factory, middleware, health endpoints
├── config.py             environment-aware validated settings
├── db.py                 async engine, sessions, readiness, lifecycle
├── models.py             SQLAlchemy models and relational constraints
├── auth.py               bearer-token dependencies
├── security.py           password hashing and JWT operations
├── exception_handlers.py consistent API errors
├── middleware.py         request context and rate limiting
├── routers/              auth, user, and item HTTP APIs
├── schemas/              Pydantic request/response contracts
├── services/             domain and transaction logic
└── tests/                async API tests
```

## Production boundaries

No platform can meaningfully provide “all FastAPI features.” This repository
now provides a strong service baseline. Before a real production launch, add
versioned migrations (for example, Alembic), a production database, centralized
metrics/tracing/log aggregation, shared rate limiting, secret-manager-backed
key rotation, and refresh-token/revocation flows according to the product’s
requirements.
