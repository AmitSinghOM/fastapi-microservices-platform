# Delivery Semantics and Phase 0 SLOs

## Guarantee

The platform provides **durable, at-least-once webhook delivery** after event
acceptance. A `202 Accepted` response means the event and its initial delivery
rows committed in the same database transaction. It does not mean receivers
have processed the event.

Exactly-once delivery is not promised. A worker can successfully invoke a
receiver and fail before recording success. The expired lease is later
reclaimed and the same event may be invoked again.

## Delivery state machine

```text
                  claim/reclaim
pending ------------------------------> processing
   ^                                        |
   |                                        | 2xx
   |                                        +--------> succeeded
   |                                        |
   |                                        | retryable and budget remains
   |                                        v
   +-------------------------------- retry_scheduled
                                            |
                                            | terminal or budget exhausted
                                            v
                                           dead

dead/succeeded -- explicit replay --> new pending delivery
```

Allowed transitions:

| Current state | Next state | Cause |
| --- | --- | --- |
| `pending` | `processing` | Worker acquires a lease |
| `retry_scheduled` | `processing` | Retry becomes due and is leased |
| `processing` | `processing` | Expired lease is reclaimed with a new token |
| `processing` | `succeeded` | Receiver returns a 2xx response |
| `processing` | `retry_scheduled` | Retryable failure and budget remains |
| `processing` | `dead` | Terminal failure or retry budget exhausted |
| `dead` or `succeeded` | new `pending` row | Explicit audited replay |

Terminal rows are not reset in place. Replay creates a new delivery linked to
the original so historical attempts remain append-only.

## Lease and crash behavior

A claim transaction changes a due delivery to `processing`, increments its
attempt number, and assigns a unique lease token and expiry using database
server time. Workers claim only currently free execution slots, so leased work
does not wait behind a local semaphore. Outbound HTTP occurs after the claim
transaction commits. A heartbeat renews ownership during the bounded overall
attempt deadline. Finalization succeeds only when the row is still `processing`,
the lease token matches, and the lease has not expired. Renewal and finalization
use database time so host clock skew cannot extend ownership.

Each delivery stores the endpoint URL, active state, public ID, and signing-secret
version observed when the event was accepted. The event stores the exact
canonical envelope bytes. Endpoint edits, deactivation, secret rotation, or JSON
re-serialization therefore cannot change accepted work. Explicit replay copies
the original delivery snapshot; operators must create a new event to target a
new endpoint configuration.

| Crash point | Durable state | Recovery behavior |
| --- | --- | --- |
| Before claim commit | Due state | Another worker claims normally |
| After claim, before HTTP | `processing` | Lease expires and another worker reclaims |
| During HTTP | `processing` | Outcome is unknown; lease expiry permits retry |
| After receiver 2xx, before finalize | `processing` | Retry may produce a duplicate invocation |
| After finalize commit | Terminal/retry state | Normal processing continues |

A stale worker cannot finalize after its lease expires or another worker
reclaims the row. Lease duration is validated to exceed the total HTTP attempt
deadline, one heartbeat scheduling delay, and the finalization margin. On
shutdown, the worker stops claiming, drains in-flight attempts for a bounded
grace period, and releases canceled claims for immediate recovery. Cancellation
after receiver acceptance can still produce a duplicate, as required by the
at-least-once contract.

## Receiver contract

Receivers must:

1. Read the exact raw request bytes before parsing JSON.
2. Verify `Webhook-Signature` over `timestamp + "." + raw_body` using a
   constant-time comparison.
3. Reject timestamps outside their configured tolerance.
4. Deduplicate using `Webhook-Id` in durable storage.
5. Make event handling idempotent, including retries after partial processing.
6. Return a 2xx response only after durable acceptance.
7. Process expensive work asynchronously when possible.

The event ID remains stable across automatic attempts. An explicit replay has a
new delivery identity linked to the original delivery; the event identity stays
stable so receiver deduplication policy remains explicit.

## Provisional Phase 0 SLOs

These targets are hypotheses until PostgreSQL baseline and pilot measurements
are recorded. They must not be published as achieved capacity.

| Signal | Initial target |
| --- | --- |
| Event acceptance availability | 99.9% monthly, excluding invalid/quota requests |
| Event acceptance latency | p95 under 250 ms at declared baseline load |
| Oldest due-delivery age | p95 under 30 seconds during normal load |
| Successful delivery latency | p95 under 60 seconds for healthy receivers |
| Accepted-event durability | No unexplained loss in one million generated jobs |
| Cross-tenant failure isolation | Healthy queue age under 2x baseline |
| Stale worker finalization | Zero accepted stale finalizations |

Receiver latency and receiver outages are reported separately from platform
latency so an unhealthy destination does not hide control-plane health.

## Baseline measurement protocol

Record all environment assumptions with each run:

- Application commit and dependency lock versions
- Host CPU, memory, operating system, and network
- PostgreSQL version, CPU, memory, storage, and connection limits
- API and worker replica counts and worker concurrency
- Event rate, fan-out distribution, payload size, receiver latency/failure mix
- Retention state and current table/index sizes

Measure API p50/p95/p99 latency, accepted/rejected events, enqueue and completion
rates, runnable depth, oldest due age, attempts per success, worker utilization,
database pool wait, query latency, connections, WAL bytes, dead tuples, and
autovacuum activity.

## Test commands

Fast deterministic suite:

```bash
pytest app/tests -m "not postgres" -q
```

Opt-in PostgreSQL concurrency suite against a disposable database or a role that
can create and drop schemas:

```bash
export TEST_POSTGRES_URL='postgresql+asyncpg://user:password@localhost/test_db'
pytest app/tests -m postgres -q
```

The fixture creates and drops a random schema. It never drops the database or
its public schema. Do not point it at production.
