# FastAPI Microservices Platform

[![CI](https://github.com/AmitSinghOM/fastapi-microservices-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/AmitSinghOM/fastapi-microservices-platform/actions/workflows/ci.yml)

**A self-hosted, PostgreSQL-native webhook delivery platform** — an
open-source alternative to hosted webhook services in the spirit of Svix and
Convoy, built for teams that want durable, multi-tenant event delivery
without operating Redis or Kafka. PostgreSQL provides transactional state,
delivery scheduling, fairness, retries, dead-letter operations, and lifecycle
management. The repository includes a Python SDK, CLI, operational portal,
and independently scalable API and worker runtimes.

Additional brokers are deliberately deferred until measured throughput,
isolation, or retention requirements justify their operational cost. Existing
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

Then open **<http://localhost:8000/portal>** — the built-in web UI — to do
the entire setup below without touching `curl`.

Docker Compose starts PostgreSQL 17.2, runs the one-shot migration, waits for a
healthy API, then starts the worker:

```bash
docker compose up --build
```

Compose defaults are explicitly development-only, not production secrets.
Provide stable `SECRET_KEY`, `API_KEY_PEPPER`, `WEBHOOK_SIGNING_KEY`, and database
credentials in every shared environment.

## Built-in web UI

Every install ships an operator portal at `/portal` — no separate frontend
to deploy. It covers the full loop: register/log in, create organizations,
projects, and producer keys, create endpoints (including event-type filters
and the signature-scheme selector), send test events, inspect deliveries and
attempts, and replay failures. It is deliberately dependency-free and
same-origin: plain HTML/JS under a strict CSP, bearer tokens held only in
page memory, one-time secrets shown once with an explicit clear button.
Disable it with `PORTAL_ENABLED=false` when operators use only the API and
[`webhookctl`](docs/phase8-adoption.md).

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
# event_types is optional: omit it to receive every event, or subscribe to
# exact types and trailing "prefix.*" wildcards.
curl -sS -X POST "$BASE/v1/projects/$PROJECT/endpoints" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://receiver.example/webhooks","description":"primary",
       "event_types":["order.*","user.created"]}'

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

Endpoints may subscribe to specific event types. A missing or null
`event_types` receives every event; a list (up to 100 validated, deduplicated
entries) restricts fan-out to exact matches and trailing `prefix.*` wildcards,
where `order.*` matches `order.created` but neither `order` nor
`orders.created`. Filtering happens once at acceptance, before admission, so
quotas charge only for deliveries actually created and accepted deliveries
are never re-filtered by later subscription edits. An event matching no
endpoint is still accepted and retained with zero deliveries.

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

Because every delivery leaves through that single egress proxy, giving the
proxy (or its NAT gateway) one or more static public IPs gives the platform a
static-source-IP story: receivers behind corporate firewalls can allowlist
those addresses and reject webhook traffic from anywhere else. This is a
deployment property, not application configuration — publish the egress
addresses to your receivers and keep them stable across scaling events, since
workers themselves never connect out directly.

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

## Tenant security and lifecycle

Organization and project roles now enforce least privilege across management,
credentials, endpoint operations, replay, and retention. Producer API keys are
scoped and expiring, support bounded-overlap rotation, and remain one-time
plaintext values. Endpoint signing versions have explicit overlap windows while
accepted deliveries retain their immutable version snapshots.

Administrative mutations append sanitized immutable audit events. Login
brute-force protection is per-account and database-backed, so its failure
budget holds across API replicas and restarts: repeated failures inside a
rolling window lock the account scope and return `429` with `Retry-After`
without evaluating credentials, a successful login clears the state, and
unknown addresses lock identically so the throttle is not an enumeration
oracle. The in-process request rate limiter remains as defense in depth.
Per-tenant policies independently bound payload and receiver-response
retention; the
existing worker clears expired content and performs grace-delayed organization
cleanup in bounded transactions. Organization export excludes payloads,
responses, destinations, and credential material. See
[the Phase 7 contract](docs/phase7-tenant-lifecycle.md) for the role matrix,
rotation rules, deletion workflow, and encryption/key-management guidance.

## Developer adoption

A standalone Python package under `sdk/python` provides a synchronous producer,
raw-byte receiver verification, a SQLAlchemy transactional outbox relay, and the
`webhookctl` management CLI. It is currently installable from the repository;
package-index publication remains pending the Phase 8 release gate.

```bash
python -m pip install './sdk/python[outbox]'
webhookctl auth login --email owner@example.com
webhookctl organizations list
webhookctl deliveries list --project <project-id>
```

Producer calls require a caller-supplied stable idempotency key and never retry
implicitly. Receiver helpers verify timestamped HMAC signatures over exact body
bytes before parsing and support a pluggable durable event-ID claim. Optional
`cloudevents` mode emits CloudEvents 1.0 structured JSON while `native` remains
the unchanged default. Runnable producer and durable-inbox receiver examples are under `examples/`.
The transactional outbox relay is deliberately this platform's answer to
broker-ingest features elsewhere: instead of consuming Kafka/SQS/RabbitMQ
topics, enqueue the event in the same database transaction as your business
write and let the relay deliver it with the same idempotency key — which
keeps exactly-one-enqueue semantics that broker bridges cannot offer.
The [built-in web UI](#built-in-web-ui) covers the same setup, test-event,
inspection, and replay flow for operators who prefer a browser.

See [the wire protocol](docs/wire-protocol.md),
[benchmarks and evidence](docs/benchmarks.md),
[Phase 8 adoption guide](docs/phase8-adoption.md),
[usability study protocol](docs/phase8-usability-study.md), and
[migration guide](docs/migration-guide.md). The amended Phase 8 completion
gate — a scripted clean-environment install and signed-delivery run
(`scripts/phase8_clean_machine_gate.py`) — passed on 2026-09-08; the former
independent-developer study was descoped, so first-time human usability
remains unvalidated. The signed release candidate and package-index
publication are still outstanding, so Phase 9 has not started.

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
`0006` adds bounded W3C trace context to accepted events. Revision `0007`
adds role assignments, scoped key and endpoint-secret lifecycles, immutable
audit history, retention policy, and organization cleanup state. Revision
`0008` adds an immutable `native|cloudevents` envelope selector and refuses an
unsafe downgrade while CloudEvents rows exist. Revision `0009` adds shared
per-account login-throttle state. Revision `0010` adds nullable per-endpoint
event-type subscriptions; existing endpoints keep receiving every event.
Set `AUTO_CREATE_SCHEMA=false`
in staging/production;
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
