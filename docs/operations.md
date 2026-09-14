# Operations: the contract on one page

What the platform guarantees, what it does not, the current SLO status, the
knobs that bound work, and what to alert on. Every default below is the
value in `app/config.py` at the commit this page ships with; every
measurement is from [the benchmarks page](benchmarks.md).

## Guarantees

- **At-least-once delivery.** An accepted event is delivered to each
  subscribed endpoint at least once or ends in a terminal state with a
  recorded reason. Receivers must deduplicate on `Webhook-Id` (the SDK's
  `verify_and_claim` and the example receiver show the pattern).
- **Accepted work is immutable.** The canonical envelope bytes, endpoint
  URL, secret version, and signature scheme are snapshotted at acceptance.
  Editing an endpoint never changes the bytes or signature of accepted or
  replayed deliveries.
- **No duplicate finalization.** Claims are `FOR UPDATE SKIP LOCKED` with
  database-time leases (`WORKER_LEASE_SECONDS`, default 60) and
  token-guarded heartbeats; a stale worker cannot finalize work it lost.
  The one-million-delivery gate observed zero accepted stale finalizations.
- **Bounded retries.** Only network errors, timeouts, and HTTP 408, 425,
  429, and 5xx retry. Other statuses are terminal. Backoff is capped
  exponential with full jitter (`WEBHOOK_BACKOFF_BASE_SECONDS` 1.0,
  `WEBHOOK_BACKOFF_CAP_SECONDS` 3600); `Retry-After` is honored up to
  `WEBHOOK_RETRY_AFTER_MAX_SECONDS` (3600). Work stops at
  `WEBHOOK_MAX_ATTEMPTS` (8) or `WEBHOOK_MAX_DELIVERY_AGE_SECONDS` (86400),
  whichever comes first, with a fixed dead reason.
- **Egress is fenced.** Application-level DNS/IP checks plus the
  CONNECT-only egress proxy deny private, link-local, metadata, and reserved
  ranges. Deliveries never leave through an unproxied path in the Compose
  and reference deployments.
- **Secrets appear once.** Endpoint signing secrets and API keys are
  returned in plaintext only at creation or rotation.

## Non-guarantees (say these out loud in a pilot review)

- **Not exactly-once.** Receivers dedupe.
- **Ordering is not guaranteed** across deliveries, including for one
  endpoint. This is a consequence of the design rather than a documented
  wire-protocol rule: each delivery retries on its own capped, jittered
  schedule, so a retried delivery can arrive after a later one succeeded.
  Receivers that need order must sequence on payload content, not arrival.
- **Queue-age SLO is not met.** Provisional target 30 s p95; measured under
  the one-million-job single-runtime run: p50 87.5 s, p95 134.5 s,
  p99 138.2 s. No queue-age SLO is claimed until a measured run meets it.
- **Single-region, single-database.** No multi-region failover story yet.
- **Young project, one maintainer.** Security fixes go to the latest 4.x
  only ([release policy](release-policy.md)).

## Capacity knobs that bound work

| Setting | Default | Bounds |
| --- | --- | --- |
| `WORKER_CONCURRENCY` | 10 | in-flight deliveries per worker process |
| `WORKER_LEASE_SECONDS` | 60 | claim lease; heartbeats extend it |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | 45 | drain window on SIGINT/SIGTERM |
| `WEBHOOK_MAX_ATTEMPTS` | 8 | per delivery |
| `WEBHOOK_MAX_DELIVERY_AGE_SECONDS` | 86400 | per delivery, independent of attempts |
| `ENDPOINT_RETRY_RATE_PER_SECOND` / `ENDPOINT_RETRY_BURST` | 0.1 / 10 | per-endpoint retry pacing |
| `DELIVERY_RETENTION_DAYS` | 30 | delivery rows |
| `DEFAULT_PAYLOAD_RETENTION_DAYS` / `DEFAULT_RESPONSE_RETENTION_DAYS` | 30 / 30 | tenant-overridable payload and response bodies |

Scale API and worker replicas independently; each replica must fit within
database connection, egress proxy, and NAT budgets. Workers never claim more
rows than they have free execution slots.

## Health

- `GET /readyz` — readiness (200 `ready` / 503 `not_ready`); wire it to load
  balancers and orchestrators.
- `GET /health` — liveness.
- Prometheus metrics are exported by the API and worker; Grafana dashboards
  ship in `deploy/observability/grafana/`: `queue-age`, `delivery-latency`,
  `failure-isolation`, `database-health`.

## What to alert on

| Signal (metric) | Why it matters | Starting threshold |
| --- | --- | --- |
| `webhook_queue_oldest_due_age_seconds` | Backlog is aging; deliveries late | > 120 s for 5 min (tighten once the SLO is met) |
| `webhook_queue_depth` | Sustained growth means workers are under-provisioned | rising for 15 min |
| `webhook_delivery_failures_total` rate by reason | Receiver outage or misconfiguration | any `dead` reason spike |
| `webhook_circuit_endpoints` | Endpoints held open by the circuit breaker | > 0 for 10 min |
| `webhook_worker_utilization_ratio` | Saturation | > 0.9 for 10 min |
| `webhook_db_pool_acquisition_seconds` p95 | Database pool starvation | > 0.5 s |
| `webhook_security_denies` | SSRF/egress denials — misconfigured endpoint or probing | any sustained rate |
| `webhook_observability_collection_failures_total` | Metrics themselves failing | > 0 |

Thresholds are starting points for a pilot, not tuned SLO alerts; the
queue-age threshold in particular reflects the measured p95 above, not the
target.

## Operational runbooks

- Upgrades and version policy: [release policy](release-policy.md)
  (4.0 upgrade notes included).
- Egress boundary and proxy: [phase 3 egress boundary](phase3-egress-boundary.md).
- Security boundaries and accepted risks: [threat model](threat-model.md).
- Dead-letter replay and lifecycle: see the portal or `webhookctl replay`.
