# Phase 4 admission control and tenant fairness

## Scope

Phase 4 keeps PostgreSQL as the durable queue and the single coordination
boundary. It adds replica-shared admission, deployment-wide worker capacity,
and tenant/endpoint-fair claiming without Redis, a broker, or service
separation.

## Admission contract

Each organization has PostgreSQL-persisted token balances for accepted events,
created deliveries, and replays. Each endpoint has a persisted delivery-rate
balance. Admission uses database time and row locks, so API and worker replicas
observe one shared budget rather than independent process-local limits.

Event ingestion checks, in order:

1. payload and idempotency-key validity;
2. an existing idempotent result, without charging quota;
3. the global and tenant locks followed by a second idempotency check;
4. fan-out, backlog, oldest-due age, and retained-byte limits;
5. event and delivery token balances; and
6. one transaction containing the event, immutable envelope, and fan-out.

The second idempotency check prevents concurrent repeats from consuming two
budgets. A uniqueness race rolls back the transaction, including token changes.
The retained-byte limit counts canonical event envelopes owned by the tenant.
The backlog includes `pending`, `processing`, and `retry_scheduled` work. The
oldest-due threshold includes runnable pending/retry work and expired processing
leases.

Endpoint creation and reactivation serialize on tenant state before counting
active endpoints. Reactivation excludes its own endpoint ID, making concurrent
retries idempotent. Replay consumes both replay and delivery tokens and applies
the same global saturation checks.

## Error contract

Tenant-specific exhaustion returns HTTP `429` with code `QUOTA_EXCEEDED`, a
stable quota name in `error.details.quota`, and integer `Retry-After` bounded by
`QUOTA_RETRY_AFTER_MAX_SECONDS`. This covers event, delivery, replay, fan-out,
endpoint-count, and retained-byte limits.

Global backlog or oldest-due protection returns HTTP `503` with code
`SERVICE_SATURATED` and a stable reason. It intentionally omits `Retry-After`:
the service cannot predict when shared saturation clears, and a common retry
time could produce a synchronized retry spike. Oversized or invalid payloads
remain HTTP `400` validation failures rather than quota responses.

A configured maximum is inclusive. For example, admission may create work that
brings backlog exactly to `GLOBAL_MAX_BACKLOG`; work that would exceed it is
rejected.

## Fair claim algorithm

Claims execute in a short transaction:

1. lock the singleton scheduler row;
2. subtract all live, unexpired processing leases from global capacity;
3. scan a bounded due window ranked first within endpoint and then tenant;
4. lock candidates with `FOR UPDATE SKIP LOCKED`;
5. lock candidate tenant and endpoint quota rows in numeric order;
6. rotate after persistent tenant and endpoint cursors; and
7. select only rows inside global, tenant, endpoint, and endpoint-rate budgets.

The singleton scheduler lock makes global capacity exact across worker
replicas. It is deliberately short and never surrounds outbound HTTP. Persistent
cursors ensure repeated one-slot claims rotate to another tenant and endpoint
rather than restarting with the oldest tenant on every poll. Expired leases are
eligible for fair reclaim; unexpired leases count against all applicable
concurrency limits.

`WORKER_CANDIDATE_SCAN_LIMIT` bounds sorting and row locking. Operators should
measure scheduler transaction latency and increase it only when tenant count or
endpoint fan-out demonstrates that the default window is insufficient.

## Deployment budgets

The maximum configured PostgreSQL connections are:

```text
API_REPLICA_COUNT * (API_DB_POOL_SIZE + API_DB_MAX_OVERFLOW)
+ WORKER_REPLICA_COUNT * (WORKER_DB_POOL_SIZE + WORKER_DB_MAX_OVERFLOW)
<= DATABASE_CONNECTION_BUDGET
```

The following hierarchy is also validated at startup:

```text
WORKER_GLOBAL_CONCURRENCY
<= WORKER_REPLICA_COUNT * WORKER_CONCURRENCY
WORKER_GLOBAL_CONCURRENCY <= WORKER_EGRESS_CONNECTION_BUDGET
ENDPOINT_CONCURRENCY <= TENANT_IN_FLIGHT_DELIVERIES
TENANT_IN_FLIGHT_DELIVERIES <= WORKER_GLOBAL_CONCURRENCY
TENANT_FANOUT_PER_EVENT <= TENANT_ENDPOINTS_PER_PROJECT
TENANT_FANOUT_PER_EVENT <= TENANT_DELIVERY_BURST
```

Reserve PostgreSQL connections for migrations, monitoring, and administrative
recovery outside `DATABASE_CONNECTION_BUDGET`. The egress budget must reflect
proxy, NAT, file-descriptor, and receiver constraints; it is not permission to
increase concurrency until those resources are measured.

## PostgreSQL evidence

The live suite uses a random disposable schema per test and removes only that
schema. It proves:

- two distinct concurrent ingests with event burst one produce one accepted
  event/delivery and one `QuotaExceededError`;
- competing worker instances collectively respect the exact global cap, each
  tenant cap, and each endpoint cap;
- a 50-job excessive tenant cannot prevent a healthy tenant from being selected
  by claim position two; and
- healthy oldest-job age at selection remains below twice its isolated baseline.

A populated disposable database upgraded from revision `0003` to `0004` with
its delivery organization ID and global, tenant, and endpoint state backfilled.
`alembic check` reported no schema drift. SQLite tests cover API response
contracts and deterministic scheduling, but PostgreSQL evidence is authoritative
for lock and skip-locked behavior.

Run the evidence locally with:

```bash
TEST_POSTGRES_URL=postgresql+asyncpg:///postgres \
  pytest app/tests/test_postgres_concurrency.py -q
```

## Security and operational review

Quota state and scheduler rows contain internal numeric identifiers but no
credentials, payloads, receiver URLs, or secrets. Public errors expose only
fixed low-cardinality quota/reason names. Bounded retry hints reduce accidental
client pressure; global `503` responses omit a speculative recovery time.

All claim and admission locks are held only for database work, with a consistent
global-before-tenant-before-endpoint order where multiple classes are needed.
Candidate scans and token arithmetic are bounded. The singleton rows are an
intentional safety serialization point and a possible throughput bottleneck;
monitor their transaction and lock-wait latency before increasing replicas.
Do not weaken caps to hide database contention. Revisit indexing, retention,
and measured PostgreSQL capacity first, and apply the broker adoption gate only
when its documented conditions are met.
