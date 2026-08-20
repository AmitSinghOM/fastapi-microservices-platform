# Phase 5 retry, dead-letter, and replay contract

## Retry classification

Outbound delivery is idempotent only under the documented receiver contract:
receivers deduplicate `Webhook-Id`. The worker retries only transient failures:
network/timeout failures and HTTP `408`, `425`, `429`, and `5xx`. A normal
non-retryable `4xx` response is terminal. Proxy policy denial, unsafe target
validation, invalid signing input, and acceptance-time inactive endpoints are
also terminal rather than repeatedly retried.

A response may provide `Retry-After` as non-negative delta-seconds or an HTTP
date. Invalid values are ignored. Valid values are clamped to
`WEBHOOK_RETRY_AFTER_MAX_SECONDS`; the scheduled delay is the larger of the
clamped value and capped exponential backoff with full jitter. No delay can
extend a delivery beyond `WEBHOOK_MAX_DELIVERY_AGE_SECONDS`.

Maximum attempts and maximum age are independent terminal boundaries. Dead
rows store `dead_at` and a fixed reason such as `max_attempts`,
`max_delivery_age`, or `non_retryable_http`. The exact attempt result remains in
the append-only attempt record. A stale delivery is rejected before outbound
HTTP and checked again under the finalization lock.

## Retry-storm protection and circuits

Every endpoint has a PostgreSQL-persisted retry token bucket. A retry claim
consumes one token; an original attempt never consumes retry budget. Time
refills are bounded by `ENDPOINT_RETRY_BURST`, and successful requests replenish
a configured amount. The state is locked in the same short transaction that
claims work, so all worker replicas share one budget.

Retryable failures increment an endpoint failure counter. At the configured
threshold the circuit opens until `ENDPOINT_CIRCUIT_OPEN_SECONDS` elapses. Open
endpoints are excluded from claims. Once elapsed, the scheduler atomically moves
the circuit to `half_open` and reserves one delivery ID as the sole probe.
Competing workers cannot reserve another probe. A successful or ordinary
non-retryable HTTP probe closes the circuit; a transient probe failure reopens
it. A crashed probe can reclaim the same expired delivery lease.

Endpoint pause is separate from circuit state and endpoint lifecycle. Pause
stops new dispatch while preserving accepted queue rows. Resume clears only the
pause. Manual circuit recovery resets failure state and retry tokens but does
not resume a paused endpoint. Already leased HTTP attempts are not interrupted.

## Dead-letter operations

Project membership scopes every dead-letter query. Operators can filter dead
rows by acceptance-time endpoint public ID, fixed dead reason, and minimum age.
Ordering is stable by `dead_at` and internal ID. Export is capped at 1,000 rows
per request and contains only delivery ID, endpoint ID, dead reason, attempt
count, last HTTP status, and timestamp. It intentionally excludes payloads,
response bodies, URLs, credentials, and signing material.

A pending or retry-scheduled row can be canceled. Processing rows cannot be
canceled because their receiver outcome is unknown; the caller receives a
conflict instead of racing token-guarded finalization. Cancellation is terminal
and records time plus an optional bounded operator reason.

## Audited replay

`POST /v1/projects/{project_id}/replays` requires `Idempotency-Key` and one to
the configured maximum distinct dead delivery IDs. Under the global/tenant
admission lock it:

1. rechecks replay-operation idempotency;
2. locks and verifies every source belongs to the authorized project and is
   dead;
3. consumes replay and delivery quota for the whole batch;
4. copies each immutable event/endpoint snapshot; and
5. writes an actor-attributed replay audit row.

All five steps commit atomically. Repeating a key with the same ordered source
list returns the original operation and consumes no quota. Reusing it with a
different list returns `409 CONFLICT`. Audit rows store public source and
created delivery IDs, counts, actor, project, organization, mode, key, and time;
they do not contain payload or receiver response data.

The compatibility single-delivery replay route remains available. It is audited
with a generated operation key, but clients requiring safe request retries
should use the idempotent replay-operation endpoint.

## Retention and purge

`DELIVERY_RETENTION_DAYS` defines the minimum age for deleting terminal
`succeeded`, `dead`, or `canceled` rows. Purge requires organization-owner
membership, defaults to dry-run, and processes at most the smaller of the
request limit and `DELIVERY_PURGE_BATCH_SIZE`. Each call uses an ordered,
bounded transaction. Delivery-attempt rows are removed by the existing foreign
key cascade; replay audit metadata remains but references deliveries by public
ID rather than a destructive foreign key.

Operators should preview, export if required, then purge repeatedly until the
preview returns zero. Production automation must retain dry-run review,
database backups, and deployment-specific retention approval.

## Evidence

Focused tests cover transient/terminal status classification, delta/date/invalid
and clamped `Retry-After`, maximum-age no-send behavior, delay precedence,
circuit opening, one half-open probe, automatic success recovery, dead filters,
CSV export, audited replay idempotency, pause/resume/manual recovery, cancel,
and dry-run/owner purge behavior.

Live PostgreSQL races prove two workers share one endpoint retry bucket: ten due
retries with burst two produce exactly two claims. A separately expired open
circuit produces exactly one half-open probe across competing workers. A
populated revision `0004` database upgraded to `0005`, backfilled legacy dead
and endpoint circuit state, downgraded to `0004`, upgraded again, and reported
no Alembic drift.

These controls follow the safe-retry principle: retry transient failures only
at an idempotent boundary, cap attempts and age, use jitter, and enforce a
shared token budget so persistent downstream failure reduces rather than
multiplies load.
