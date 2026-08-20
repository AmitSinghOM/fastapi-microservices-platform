# Phase 6 observability and bounded autoscaling

## Contract

Phase 6 exposes Prometheus text metrics from API `/metrics` and the worker's
private metrics listener. OpenTelemetry exports sampled OTLP/HTTP spans when an
endpoint is configured. W3C `traceparent` and `tracestate` are persisted on the
accepted event and resumed by delivery workers. Telemetry failure never changes
event acceptance or delivery correctness.

Only fixed, documented labels are allowed: runtime role, normalized route,
method, status class, event outcome/reason, queue kind, delivery action/outcome,
failure class, circuit state, and database operation/state. Tenant, project,
event, delivery and endpoint identifiers; event types; idempotency keys; SQL;
URLs; credentials; payloads; authorization headers; signing material; and
response bodies are forbidden metric labels. High-cardinality identifiers may
appear only in access-controlled traces or structured audit logs; sensitive
values remain forbidden there too.

Database-global gauges exported by multiple replicas are combined with `max`,
never `sum`. Rates are computed from monotonic counters with PromQL `rate()`.
The queue collector queries PostgreSQL as the source of truth; process-local
counters are not queue depth.

## Metric surface

Counters are `webhook_http_requests_total`, `webhook_events_total`,
`webhook_delivery_lifecycle_total`, `webhook_outbound_responses_total`,
`webhook_delivery_failures_total`, and
`webhook_observability_collection_failures_total`. Histograms cover HTTP,
database query/pool acquisition, attempt, end-to-end, and attempts-per-success
latency. Gauges cover queue kinds, oldest due age, aggregate circuit states,
worker in-flight/utilization, pool use, and fixed security-denial classes.

Event outcomes are `accepted`, `idempotent`, `rejected`, or `throttled` with
fixed reasons. Delivery failures collapse into `dns`, `tls_or_connect`,
`timeout`, `policy_denied`, `http`, `max_age`, and `other`. HTTP labels contain
only normalized route templates and status classes.

## SLO and burst declaration

The provisional service objectives are: event acceptance p95 below 250 ms,
99.9% monthly control-plane availability, oldest due age below 30 seconds, and
successful delivery p95 below 60 seconds excluding receiver-directed retry
delays. Phase 6 declares a 10× burst as 10,000 fan-out-one events arriving over
60 seconds against a 1,000-event/minute steady baseline. The drain objective is
five minutes from the end of arrival, with no pool timeout, no accepted stale
finalization, and healthy-tenant latency below the greater of twice its
isolated baseline or a 250 ms scheduling-noise floor.

Autoscaling recommendations use the maximum desired replica count implied by
oldest due age, runnable backlog per worker, arrival/completion rate imbalance,
and worker utilization. Scale-down requires a ten-minute stable window. The
configured maximum is rejected when it exceeds database pool, worker egress, or
NAT connection budgets. Destination and global concurrency remain authoritative
regardless of replica count.

## PostgreSQL operations

The application exports pool use, acquisition latency, and fixed-operation
query latency without SQL text. The PostgreSQL exporter uses a read-only
monitoring role and supplies connections, locks, WAL, I/O, relation growth,
dead tuples, and autovacuum statistics. Grant only `pg_monitor` and CONNECT to
the target database; do not reuse application credentials.

## Reproducible evidence

Run only against a local PostgreSQL instance; the harness refuses remote hosts,
requires `--confirmation PHASE6_LOCAL_ONLY`, creates a random schema, and drops
only that schema:

```bash
PHASE6_POSTGRES_URL='postgresql+asyncpg://USER@/postgres?host=/tmp' \
  python scripts/phase6_benchmark.py --confirmation PHASE6_LOCAL_ONLY
```

The 2026-08-20 run admitted 10,000 burst events plus two healthy probes. All
10,002 deliveries succeeded with 10,002 attempts and zero attempt mismatch.
The post-arrival drain was 7.49 seconds against 300 seconds; peak oldest due
age was 8.67 seconds. Burst-time healthy latency was 51 ms against the 250 ms
objective. The five-connection pool reached five checked-out connections but
had no timeout, overflow, or unfinished delivery. The disposable schema was
removed. Docker and `promtool` were unavailable; JSON and YAML syntax, focused
application tests, and PromQL review were used locally, while CI should run
`promtool check rules` when the binary is available.
