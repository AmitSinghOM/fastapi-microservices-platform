# FastAPI Webhook Delivery Platform

An async webhook control plane and separately runnable delivery worker. Existing
JWT users and owned-item APIs remain available; webhook ingestion uses project
API keys and organization membership is the management authorization boundary.

## Architecture

```text
JWT client -> FastAPI control plane -> PostgreSQL/SQLite
producer --X-API-Key--> POST /v1/events -> event + delivery fan-out transaction
worker -> short lease claim -> HTTPS outside transaction -> guarded finalize
```

The database is the durable queue. Deliveries use
`pending|processing|retry_scheduled|succeeded|dead`, lease tokens, and append-only
attempt records. API keys are stored only as peppered HMAC-SHA256 digests.
Endpoint secrets are derived from a dedicated signing key, endpoint public ID,
and secret version. Plaintext credentials appear only in create/rotation
responses.

## Quick start

Python 3.11+:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
# Separate terminal; never embed this in the API process:
python -m app.worker
```

Docker Compose starts PostgreSQL 17.2, runs the one-shot migration, waits for a
healthy API, then starts the worker:

```bash
docker compose up --build
```

Compose defaults are explicitly development-only, not production secrets.
Provide stable `SECRET_KEY`, `API_KEY_PEPPER`, `WEBHOOK_SIGNING_KEY`, and database
credentials in every shared environment.

## End-to-end API

```bash
BASE=http://localhost:8000
curl -sS -X POST "$BASE/users/" -H 'Content-Type: application/json' \
  -d '{"email":"owner@example.com","name":"Owner","password":"long-password"}'
TOKEN=$(curl -sS -X POST "$BASE/auth/login" \
  -d 'username=owner@example.com&password=long-password' | jq -r .access_token)

ORG=$(curl -sS -X POST "$BASE/v1/organizations" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Example"}' | jq -r .public_id)
PROJECT=$(curl -sS -X POST "$BASE/v1/organizations/$ORG/projects" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Production events"}' | jq -r .public_id)
API_KEY=$(curl -sS -X POST "$BASE/v1/projects/$PROJECT/api-keys" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"producer"}' | jq -r .plaintext_key)

# Use a public HTTPS receiver whose DNS resolves only to global addresses.
curl -sS -X POST "$BASE/v1/projects/$PROJECT/endpoints" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://receiver.example/webhooks","description":"primary"}'

curl -sS -X POST "$BASE/v1/events" \
  -H "X-API-Key: $API_KEY" -H 'Idempotency-Key: order-123-created' \
  -H 'Content-Type: application/json' \
  -d '{"type":"order.created","payload":{"order_id":"123","total":42}}'
```

Event ingestion returns `202`. Reusing a key with the same type and canonical
payload returns the original event; changing either returns standardized `409
CONFLICT`. Lists use bounded `offset` and `limit` (maximum 100). Management
routes cover organizations/members/projects, key revocation, endpoint update,
deactivation and secret rotation, event/delivery detail, attempts, and replay.
Replay creates a fresh delivery linked to the original.

## Signature verification

The exact request body is compact canonical UTF-8 JSON with sorted keys and no
NaN/infinity. It is the envelope:

```json
{"created_at":"<ISO-8601>","data":<event-payload>,"id":"<event-id>","type":"<event-type>"}
```

For timestamp `T`, compute lowercase hex
`HMAC-SHA256(endpoint_secret, ASCII(T) + b"." + exact_body_bytes)`. The header is
exactly `Webhook-Signature: t=<T>,v1=<hex>`. Also sent are `Webhook-Id`,
`Webhook-Timestamp`, `Webhook-Event`, and `Webhook-Attempt`. Parse the signature,
reject stale timestamps according to receiver policy, compute over the raw body,
and compare with a constant-time function before parsing JSON.

## Retries and worker safety

Only network/timeouts and HTTP `408`, `425`, `429`, and `5xx` retry. Other HTTP
statuses are terminal. Delay uses capped exponential backoff with full jitter;
max attempts transitions to `dead`. Claims use row locking with skip-locked,
short lease transactions, and token-guarded finalization. HTTP is never inside a
database transaction. Poll batch/concurrency limits bound retry load; scale
workers horizontally against PostgreSQL. SQLite is for local/test use and does
not provide PostgreSQL's concurrent claim semantics.

The client has explicit connect/read/write/pool timeouts, ignores proxy
environment variables, refuses redirects, captures only a bounded response
prefix, and does not log payloads, credentials, endpoint secrets, or responses.
Stop with SIGINT/SIGTERM for graceful completion of the current batch.

## SSRF and production egress boundary

Targets require HTTPS. HTTP can be enabled only in development and defaults on
only there. Creation and every send resolve DNS and reject credentials,
fragments, localhost names, non-global addresses, and IPv4-mapped IPv6 private
addresses. Redirects are disabled.

Application validation is not a complete SSRF boundary: DNS can change between
validation and connection. Production must enforce external network egress
allowlisting/filtering (for example, an egress proxy or firewall), deny cloud
metadata and private networks, and control DNS. Allow only required destination
ports/domains and monitor denied traffic.

## Migrations, operations, and compatibility

Apply schema changes before API rollout:

```bash
alembic upgrade head
alembic downgrade base  # destructive; development rollback only
```

`migrations/env.py` uses the configured async database URL. Set
`AUTO_CREATE_SCHEMA=false` in staging/production; those environments reject
local schema auto-creation and require all three secrets at 32+ characters.
Configuration includes payload/response/idempotency limits, worker lease/poll/
batch/concurrency, four HTTP timeout phases, and retry/backoff bounds.

Health endpoints: `/livez`, database `/readyz`, and deprecated `/health`.
Existing `/users`, `/auth`, and `/items` routes and JWT behavior are retained,
including owned-item delete-orphan cascading. API docs are at `/docs` and
`/redoc` when enabled. CORS remains opt-in and, when configured, accepts the JWT,
API-key, idempotency, content, and request-ID headers.
