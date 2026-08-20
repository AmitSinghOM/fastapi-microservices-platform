# FastAPI Webhook Delivery Platform

[![CI](https://github.com/AmitSinghOM/fastapi-microservices-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/AmitSinghOM/fastapi-microservices-platform/actions/workflows/ci.yml)

An async webhook control plane and separately runnable delivery worker. Existing
JWT users and owned-item APIs remain available; webhook ingestion uses project
API keys and organization membership is the management authorization boundary.

## Architecture

```text
JWT client -> FastAPI control plane -> PostgreSQL/SQLite
producer --X-API-Key--> POST /v1/events -> event + delivery fan-out transaction
worker -> short lease claim -> CONNECT-only egress proxy -> HTTPS -> guarded finalize
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
statuses are terminal. Valid delta-seconds and HTTP-date `Retry-After` values are
clamped to a configured maximum and combined with capped exponential backoff
using full jitter. Maximum attempts and maximum delivery age independently
bound work; terminal deliveries retain a fixed dead reason. Claims use row
locking with skip-locked, database-time leases, token-guarded heartbeats, and
expiry-guarded finalization.
Workers never claim more rows than free execution slots. Every attempt has an
overall deadline in addition to HTTP phase timeouts; HTTP is never inside a
database transaction. On SIGINT/SIGTERM the worker stops claiming, drains work
within a configured grace period, releases unfinished claims, then closes HTTP
and its independently bounded database pool.

The event’s exact canonical envelope bytes and each delivery’s endpoint URL,
active state, public ID, and signing-secret version are captured atomically at
acceptance. Later endpoint edits or JSON re-serialization cannot alter accepted
work or signatures. Replay deliberately copies the original snapshot. Delivery
attempts remain insert-only in application behavior and uniquely numbered per
delivery; crash windows may leave intentional numbering gaps.

Retries consume a PostgreSQL-backed endpoint token bucket; original attempts do
not, so retry suppression never prevents the first delivery attempt. Repeated
transient failures open an endpoint circuit. Open endpoints dispatch no work,
an elapsed circuit permits exactly one half-open probe across all workers, and
a successful probe closes the circuit. Operators can independently pause/resume
an endpoint or manually recover its circuit.

The client has explicit connect/read/write/pool timeouts, pins the configured
HTTP CONNECT proxy, ignores proxy environment variables (including `NO_PROXY`),
refuses redirects, and disables keepalive so every attempt opens a fresh tunnel
and receives a fresh proxy DNS/policy decision. It captures only a bounded
response prefix and does not log payloads, credentials, endpoint secrets,
destinations, or responses. API-key `last_used_at` writes are coalesced in
memory and flushed periodically, so ingestion no longer waits for a usage-only
database commit and the timestamp is intentionally eventually consistent. API
and worker database pools have separate bounded size, overflow, and wait
settings. SQLite is for local/test use and does not provide PostgreSQL's
concurrent claim semantics.

## Admission control and tenant fairness

PostgreSQL-backed token buckets share event, delivery, and replay quotas across
all API replicas. Endpoint creation/reactivation, per-event fan-out, retained
event bytes, payload size, global backlog, and oldest-runnable-job age are also
bounded. Tenant exhaustion returns `429 QUOTA_EXCEEDED` with a bounded
`Retry-After`; global backlog or age protection returns `503
SERVICE_SATURATED` without encouraging synchronized retries. An idempotent
repeat returns its existing event without consuming quota again.

Workers take a short global scheduler lock, account for all unexpired leases,
then choose a bounded window with persistent tenant and endpoint round-robin
cursors. Claims obey deployment-wide, tenant, and endpoint concurrency limits
plus a shared endpoint token bucket. This keeps an older excessive backlog from
starving another tenant while preserving lease and skip-locked ownership.

Replica counts are explicit. Startup rejects configurations where multiplied
API and worker pools exceed `DATABASE_CONNECTION_BUDGET`, where global worker
concurrency exceeds total process slots or `WORKER_EGRESS_CONNECTION_BUDGET`,
or where endpoint/tenant/global concurrency limits are inconsistent. See
[the Phase 4 contract](docs/phase4-admission-fairness.md) for formulas, quota
semantics, and PostgreSQL race/fairness evidence.

## Dead-letter and replay operations

Dead deliveries can be filtered by endpoint, fixed reason, and minimum age, or
exported as bounded CSV without payloads, secrets, destination URLs, or response
bodies. The `/v1/projects/{project_id}/replays` API accepts a bounded list of
dead delivery IDs plus `Idempotency-Key`; replay admission, immutable snapshot
copies, and an actor-attributed audit record commit atomically. Repeating the
same key and source list returns the original operation, while changing the list
returns a conflict. The compatibility single-delivery replay route remains
available and is also audited.

Pending and retry-scheduled deliveries can be canceled without racing an active
lease. Endpoint pause/resume affects dispatch but not accepted immutable
snapshots. Owner-only retention purge defaults to dry-run, deletes only bounded
batches of terminal deliveries older than `DELIVERY_RETENTION_DAYS`, and relies
on database cascades for attempt cleanup. See
[the Phase 5 contract](docs/phase5-retry-dlq.md) for state transitions and
operational safeguards.

## SSRF and production egress boundary

Targets require HTTPS and effective destination port 443 outside development.
HTTP can be enabled only in development and defaults on only there. Creation
and every send resolve all DNS answers and reject credentials, fragments,
localhost names, non-global addresses, and IPv4-mapped IPv6 private addresses.
Redirects are disabled.

Compose places workers only on internal backend and worker-proxy networks. A
dedicated, digest-pinned Squid service is the sole member that also joins the
outbound network. It accepts CONNECT only from the worker network, permits only
TCP 443, independently resolves destinations, and denies private, link-local,
metadata, cluster/service/database, reserved, mapped, and local IPv6 networks.
TLS remains end to end, so HTTPX performs normal certificate and SNI checks.
The proxy is not published, runs without root or Linux capabilities on a
read-only filesystem, and does not log destination-bearing requests.

`WORKER_EGRESS_PROXY_URL` is mandatory in staging and production and must be a
credential-free internal `http://host:port` URL. Application checks remain
defense in depth; deployments that replace Compose must provide an equivalent
independent DNS and network policy. See
[the Phase 3 boundary](docs/phase3-egress-boundary.md) for the deny corpus and
validation procedure.

## Observability and bounded autoscaling

The API exports Prometheus text at `/metrics`; the worker exposes the same
fixed-label registry on its private `WORKER_METRICS_HOST` and
`WORKER_METRICS_PORT`. Configure `OTEL_EXPORTER_OTLP_ENDPOINT` to export sampled
OTLP/HTTP traces. W3C trace context is stored with accepted events and resumed
by workers, while payloads, URLs, credentials, SQL, response bodies, and
identifiers never become metric labels.

Prometheus scrape/recording/alert rules, four Grafana dashboards, the read-only
PostgreSQL monitoring role, and the bounded worker scaling policy are under
`deploy/observability`. Scale recommendations combine due age, runnable backlog
per worker, arrival/completion rates, and worker utilization; configured maximum
replicas must fit remaining database, egress, and NAT budgets. See
[the Phase 6 contract](docs/phase6-observability-autoscaling.md) for metric
semantics, SLOs, and reproducible 10× burst evidence.

## Migrations, operations, and compatibility

Apply schema changes before API rollout:

```bash
alembic upgrade head
alembic downgrade base  # destructive; development rollback only
```

`migrations/env.py` uses the configured async database URL. Revision `0003`
backfills canonical event envelopes and endpoint snapshots; historical endpoint
state can only reflect what is visible during that upgrade. Revision `0004`
backfills delivery organization ownership and initializes global, tenant, and
endpoint admission state. Revision `0005` backfills dead-letter reasons and
endpoint retry/circuit state, and creates replay audit records. Revision
`0006` adds bounded W3C trace context to accepted events. Set
`AUTO_CREATE_SCHEMA=false` in staging/production;
those environments reject local schema auto-creation and require all three
secrets at 32+ characters. Configuration includes multiplied replica/database
connection budgets, shared admission and worker concurrency limits, an explicit
worker egress proxy, API-key usage flush bounds, payload/response/idempotency
limits, worker lease/heartbeat/attempt/drain bounds, four HTTP timeout phases,
and retry/backoff bounds.

Health endpoints: `/livez`, database `/readyz`, and deprecated `/health`.
Existing `/users`, `/auth`, and `/items` routes and JWT behavior are retained,
including owned-item delete-orphan cascading. API docs are at `/docs` and
`/redoc` when enabled. CORS remains opt-in and, when configured, accepts the JWT,
API-key, idempotency, content, and request-ID headers.

## Development and project policy

Install pinned development checks and run the local gate:

```bash
python -m pip install -r requirements-dev.txt
make check
```

Set `TEST_POSTGRES_URL=postgresql+asyncpg:///postgres` to include the isolated
PostgreSQL concurrency suite. See [CONTRIBUTING.md](CONTRIBUTING.md) for review
requirements, [SECURITY.md](SECURITY.md) for private vulnerability reporting,
[the threat model](docs/threat-model.md) for security boundaries, and
[the release policy](docs/release-policy.md) for versioning and upgrades.

The inherited `/items` API is a tutorial compatibility surface rather than part
of the webhook product. Set `EXAMPLE_ITEMS_ENABLED=false` to omit its routes.
It remains enabled by default through 3.x, defaults off in 4.0, and will not be
removed before 5.0.

This project is available under the [Apache License 2.0](LICENSE). Community
participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
