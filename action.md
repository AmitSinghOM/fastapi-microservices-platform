# Webhook Platform Action Plan

## Objective

Build a security-first, PostgreSQL-first, self-hosted outbound event-delivery
platform for small and mid-sized SaaS teams. Keep one modular codebase with two
independently scalable runtimes: a stateless FastAPI control plane and delivery
workers.

The product provides at-least-once delivery. It does not claim exactly-once
delivery or unlimited scale. PostgreSQL remains the initial durable queue; a
broker is introduced only after measured limits justify the operational cost.

## Product promise

A developer should be able to install the platform, create a project and
endpoint, send a signed event, inspect attempts, and safely replay failed work
without operating Kafka, Redis, or a collection of custom retry services.

## Execution rules

1. Complete phases in order; do not begin a phase until its entry dependencies
   and previous completion gate are satisfied.
2. Preserve API compatibility unless a documented migration is included.
3. Keep database transactions short and never perform outbound HTTP inside one.
4. Treat duplicate delivery as normal and require receiver idempotency.
5. Enforce tenant and destination fairness before increasing worker counts.
6. Add infrastructure only in response to measured bottlenecks.
7. Every phase requires tests, documentation, security review, and operational
   evidence before it is marked complete.
8. Do not log API keys, signing secrets, authorization headers, complete event
   payloads, or complete receiver responses.

## Current baseline

The repository already includes API-key event ingestion, idempotency, atomic
event/delivery fan-out, PostgreSQL skip-locked claiming, leases, HMAC signing,
retry scheduling, dead delivery state, replay, bounded HTTP behavior, and
application-layer SSRF checks.

Known gaps include worker over-claiming, no lease heartbeat, no tenant-fair
scheduling, process-local admission limits, no retry budgets or circuit breaker,
incomplete production egress enforcement, insufficient PostgreSQL concurrency
tests, limited observability, and incomplete open-source governance.

## Phase 0 — Baseline, contracts, and measurements

**Purpose:** establish reproducible correctness and capacity evidence before
changing queue behavior.

- [x] Document the delivery state machine and allowed transitions.
- [x] Document at-least-once semantics and the receiver deduplication contract.
- [x] Record baseline API latency, ingest rate, delivery throughput, queue age,
      database connections, WAL volume, and worker utilization.
- [x] Add PostgreSQL integration-test infrastructure separate from fast SQLite
      tests.
- [x] Test concurrent idempotency-key submissions and payload conflicts.
- [x] Test multiple workers claiming the same due queue.
- [x] Inject crashes before HTTP, after HTTP, and before database finalization.
- [x] Verify stale lease recovery and token-guarded finalization.
- [x] Define initial SLOs for event acceptance, oldest due-job age, successful
      delivery latency, and platform availability.

**Phase 0 evidence (2026-08-18):** `docs/delivery-semantics.md` defines the
state machine, crash windows, receiver contract, provisional SLOs, and baseline
protocol. The 65-test suite passes with the opt-in live PostgreSQL concurrency
tests. Crash injection proves stale-lease recovery, stale-finalization
rejection, and the expected duplicate window after receiver success. The
reproducible local results in `docs/phase0-baseline.md` record API latency and
ingestion, real-worker and bulk throughput, queue age, worker utilization,
connections, WAL, storage, and correctness invariants. The run persisted and
completed 1,000,000 unique jobs with 1,000,000 unique attempts, no missing or
nonterminal jobs, no unexplained duplicates, and no accepted stale
finalizations. Its queue-age p95 was 134.46 seconds, so the provisional
30-second queue-age SLO remains unachieved and must not be presented as met.

**Completion gate: passed on 2026-08-18.** One million generated delivery jobs
completed without an accepted event disappearing. No crash-window duplicates
were injected in the baseline run; dedicated crash tests document and verify
the permitted at-least-once duplicate behavior.

## Phase 1 — Open-source release safety

**Purpose:** make expectations, security reporting, and contribution ownership
clear before inviting production users.

- [x] Select and add an explicit Apache-2.0 license after owner review.
- [x] Add `SECURITY.md` with supported versions and private reporting process.
- [x] Add `CONTRIBUTING.md`, code of conduct, development setup, and review
      requirements.
- [x] Add a threat model covering tenants, API keys, endpoint secrets, event
      payloads, SSRF, replay abuse, and worker compromise.
- [x] Add CI for tests, linting, type checks, migration checks, secret scanning,
      dependency review, and container build.
- [x] Define release versioning, upgrade support, and vulnerability response.
- [x] Move tutorial-only item APIs behind an example flag and document their
      planned default-disable and removal schedule.

**Phase 1 evidence (2026-08-18):** The repository now includes Apache-2.0
licensing, private security reporting, contribution and conduct policies, a
security-boundary threat model, SemVer/upgrade/deprecation policy, pinned Ruff
and mypy development checks, monthly grouped Dependabot updates, a
pull-request template, one consolidated 10-minute CI job for quality, live
PostgreSQL tests, migration drift, secret scanning, and dependency review, plus
a separate container build triggered only by container/dependency changes or
manual dispatch. Documentation-only changes skip paid runner work. `make check`
passes 63 fast tests,
Ruff, mypy, and compilation; the complete live PostgreSQL suite passes 65
tests. A disposable PostgreSQL database successfully upgraded through
`0002_drop_duplicate_uniques`, and `alembic check` found no drift. After commit
`1ce724c`, GitHub-hosted CI run `32147948666` and container run `32147948678`
both completed successfully.

**Completion gate: passed on 2026-08-18.** A new contributor can run checks
locally, submit a security report privately, and understand the project license
and compatibility policy. The required remote CI and container checks are green.

## Phase 2 — Queue correctness and worker safety

**Purpose:** make horizontal worker scaling safe before using it for spikes.

- [x] Claim no more jobs than currently available execution slots.
- [x] Add lease heartbeat/renewal for long-running requests.
- [x] Set lease duration above queue wait, HTTP deadline, heartbeat delay, and
      finalization margin.
- [x] Snapshot endpoint URL, endpoint state, and signing-secret version into the
      delivery at event acceptance time.
- [x] Preserve immutable event payload bytes or an immutable canonical envelope.
- [x] Make claim, finalization, and stale-worker rejection atomic.
- [x] Keep attempt records append-only and uniquely numbered per delivery.
- [x] Add graceful shutdown: stop claiming, finish bounded in-flight work, then
      release database and HTTP resources.
- [x] Bound API and worker connection pools independently.
- [x] Remove synchronous API-key `last_used_at` writes from the ingest hot path;
      aggregate or update them periodically.

**Phase 2 evidence (2026-08-18):** `docs/phase2-worker-safety.md` records the
ownership, immutable-input, shutdown, pool, and usage-write contracts. Workers
claim only free slots, renew live token-guarded leases, use an overall request
deadline, reject expired finalization using PostgreSQL time, and release
unfinished work after a bounded drain. Migration `0003_phase2_delivery_safety`
stores exact canonical event bytes plus acceptance-time endpoint snapshots and
backfills existing rows. Replay copies the original snapshot. API and worker
pools have independent budgets, and API-key usage writes are coalesced off the
ingest transaction. All 76 tests passed, including five live PostgreSQL races;
a populated `0002` database upgraded and backfilled successfully, `alembic
check` found no drift, and Ruff, configured mypy, compilation, and `make check`
passed.

**Completion gate: passed on 2026-08-18.** Concurrent workers cannot own the
same valid lease; expired or replaced workers cannot finalize; bounded shutdown
leaves unfinished work immediately reclaimable. Remote CI must be green after
these changes are committed and pushed before Phase 3 begins.

## Phase 3 — Egress and SSRF security boundary

**Purpose:** treat outbound connectivity as a core product security surface.

- [x] Require HTTPS in staging and production.
- [x] Continue rejecting credentials, fragments, redirects, localhost names,
      private addresses, link-local addresses, metadata targets, IPv6 local
      ranges, and IPv4-mapped private IPv6 addresses.
- [x] Revalidate DNS immediately before each attempt.
- [x] Deploy workers behind a dedicated egress proxy or equivalent firewall and
      DNS policy so application checks are not the only control.
- [x] Deny private, cluster, service, control-plane, database, and cloud metadata
      networks at the network layer.
- [x] Allow only required outbound ports, normally TCP 443.
- [x] Preserve TLS hostname verification and SNI through the egress path.
- [x] Add a bypass corpus covering alternate IP notation, CNAME chains,
      split-horizon DNS, redirects, DNS rebinding, and proxy bypass.
- [x] Emit low-cardinality security metrics and auditable deny events without
      exposing destination credentials.

**Phase 3 evidence (2026-08-19):** `docs/phase3-egress-boundary.md` defines the
application and network contracts. Staging/production require HTTPS on effective
port 443 and an explicit credential-free proxy. Workers ignore ambient proxy
variables, disable redirects and keepalive, and revalidate every DNS answer
before each attempt. Compose isolates workers on internal networks and routes
outbound CONNECT through an unprivileged, capability-free, read-only Squid
container that independently resolves destinations, permits only TCP 443, and
denies private, metadata, service, cluster, database, control-plane, mapped,
local, and reserved networks. Fixed layer/reason counters and audit records do
not accept destination context. All 100 SQLite/PostgreSQL tests passed locally;
Ruff, mypy, compilation, and diagnostics passed. The controlled Linux container
harness proved direct sockets fail, private/CNAME/mixed/split-view/rebound and
non-443 targets receive proxy denials, valid TLS/SNI succeeds, mismatched TLS is
rejected, and injected destination secrets do not appear in container logs.

**Completion gate: passed on 2026-08-19.** GitHub CI run `32301923656` and
container run `32301923677` passed on commit `46d2a15`. The network layer
blocked controlled public-first/private-second DNS rebinding and private targets
even though the probe bypassed application validation.

## Phase 4 — Admission control and tenant fairness

**Purpose:** prevent traffic spikes and noisy neighbors from overwhelming the
shared database or worker fleet.

- [x] Add shared per-tenant event-rate and burst limits.
- [x] Add limits for deliveries per second, in-flight deliveries, endpoints per
      project, fan-out per event, payload size, replay rate, and retained bytes.
- [x] Return `429` with bounded `Retry-After` for tenant quota exhaustion.
- [x] Return `503` for temporary global saturation or database protection.
- [x] Add a global maximum backlog and oldest-job-age admission threshold.
- [x] Replace global oldest-first claiming with tenant-fair scheduling, followed
      by endpoint fairness.
- [x] Add per-endpoint concurrency and rate limits.
- [x] Add a global concurrency governor tied to database and egress budgets.
- [x] Ensure API replica count cannot multiply database connections beyond the
      configured pool budget.

**Phase 4 evidence (2026-08-20):**
`docs/phase4-admission-fairness.md` defines shared token-bucket, saturation,
HTTP error, fair-claim, lock-order, and deployment-budget contracts. PostgreSQL
race tests prove distinct concurrent events share one tenant burst and competing
workers collectively obey global, tenant, and endpoint caps. A controlled
50-job excessive tenant remained unable to delay a healthy tenant beyond claim
position two, and the healthy job's age at claim remained below twice its
isolated baseline. Expired processing leases participate in oldest-due
protection, idempotent repeats do not consume quota twice, and endpoint
reactivation does not count itself against its limit. A populated disposable
PostgreSQL database upgraded from `0003` to `0004`; delivery ownership and all
quota state backfilled, and `alembic check` found no drift. Diagnostics, Ruff,
configured mypy, compilation, 104 SQLite/non-PostgreSQL tests, and the complete
112-test SQLite/PostgreSQL suite passed. The existing Passlib `crypt`
deprecation warning remains unchanged.

**Completion gate: passed on 2026-08-20.** Under the controlled excessive-tenant
workload, the healthy tenant remained within two times its normal
oldest-job-age baseline. Phase 5 must not begin until these uncommitted Phase 4
changes are reviewed and any required remote gate is explicitly authorized.

## Phase 5 — Retry policy, dead-letter operations, and replay

**Purpose:** recover predictably without generating retry storms.

- [x] Retry network failures, timeouts, HTTP 408, 425, 429, and 5xx responses.
- [x] Treat ordinary non-retryable 4xx responses as terminal.
- [x] Honor valid `Retry-After` values within a configured maximum.
- [x] Retain capped exponential backoff with full jitter.
- [x] Add maximum delivery age in addition to maximum attempts.
- [x] Add endpoint-scoped retry token buckets and circuit breakers.
- [x] Define half-open probes and automatic/manual endpoint recovery.
- [x] Build operational dead-letter queries by tenant, endpoint, reason, and age.
- [x] Add audited single and bulk replay with rate limits and idempotency.
- [x] Add pause, cancel, export, retention, and purge operations.

**Phase 5 evidence (2026-08-20):** `docs/phase5-retry-dlq.md` defines transient
classification, bounded `Retry-After`, full-jitter backoff, attempt/age limits,
shared retry tokens, circuit transitions, dead-letter operations, audited replay,
and destructive-operation safeguards. Focused tests prove maximum-age work sends
no HTTP, non-retryable statuses terminate, valid delta/date retry hints are
clamped, and successful half-open probes recover automatically. API tests prove
dead filtering/export, atomic replay audit/idempotency, pause/resume/manual
recovery, race-safe cancel, owner-only dry-run purge, and bounded retention.
Live PostgreSQL races prove ten due endpoint retries with burst two yield exactly
two claims across workers, while an elapsed open circuit yields exactly one
half-open probe. A populated `0004` database upgraded to `0005`, backfilled
legacy dead and endpoint state, downgraded, re-upgraded, and passed `alembic
check`. Diagnostics, Ruff, mypy over 13 sources, compilation, 108 fast tests,
and the complete 118-test SQLite/PostgreSQL suite passed. The existing Passlib
`crypt` deprecation warning remains unchanged.

**Completion gate: passed on 2026-08-20.** A controlled sustained-outage queue
remained inside the shared endpoint retry and concurrency budgets and could not
consume additional worker slots after circuit opening. Phase 6 must not be
marked complete until its independent observability and burst evidence exists.

## Phase 6 — Observability and autoscaling

**Purpose:** scale from evidence rather than CPU or queue depth alone.

- [ ] Export Prometheus-compatible metrics and OpenTelemetry traces.
- [ ] Measure accepted, rejected, throttled, and idempotent events.
- [ ] Measure runnable queue depth, total depth, oldest due-job age, enqueue,
      claim, completion, lease expiry, and stale-finalization rates.
- [ ] Measure end-to-end delivery latency, attempts per success, HTTP classes,
      DNS/TLS/connect/timeout failures, and circuit state.
- [ ] Measure PostgreSQL pool wait, query latency, locks, WAL/IO, table growth,
      dead tuples, and autovacuum behavior.
- [ ] Keep tenant, event, and endpoint identifiers out of unrestricted metric
      labels; place high-cardinality context in structured logs and traces.
- [ ] Create queue-age, delivery-latency, failure-isolation, and database-health
      dashboards and alerts.
- [ ] Autoscale workers using oldest due-job age, runnable backlog per worker,
      arrival/completion rates, and worker utilization.
- [ ] Bound scale-up by database connections, write capacity, egress sockets,
      NAT limits, and destination concurrency.

**Completion gate:** a declared 10× burst drains within the SLO window without
connection-pool exhaustion or unhealthy-tenant impact.

## Phase 7 — Tenant security and lifecycle

**Purpose:** make multi-tenant operation auditable and maintainable.

- [ ] Add fine-grained organization/project roles and least-privilege checks.
- [ ] Add scoped and expiring API keys with rotation overlap.
- [ ] Add endpoint signing-secret rotation with a verification overlap window.
- [ ] Add immutable administrative audit events for key, endpoint, replay,
      membership, and quota changes.
- [ ] Define payload and response retention policies by tenant plan.
- [ ] Add encryption and key-management guidance for sensitive persisted data.
- [ ] Add organization deletion/export workflows and background cleanup.

**Completion gate:** authorization, rotation, retention, and deletion behavior
are covered by tenant-isolation tests and documented operational procedures.

## Phase 8 — Developer adoption

**Purpose:** make the service easier to adopt than rebuilding webhook delivery.

- [ ] Publish a Python producer SDK with an optional transactional outbox relay.
- [ ] Publish receiver helpers for raw-body signature verification, timestamp
      tolerance, and event deduplication.
- [ ] Keep the HTTP wire contract language-neutral.
- [ ] Add CloudEvents-compatible envelopes as an optional compatibility mode.
- [ ] Add a CLI for projects, endpoints, test events, attempts, and replay.
- [ ] Provide example producer and receiver applications.
- [ ] Target signed-event delivery within 30 minutes on a clean machine.
- [ ] Add migration guides from custom workers and common queue patterns.
- [ ] Build only a minimal operational portal after API and CLI workflows are
      stable.

**Completion gate:** at least eight of ten independent developers complete the
installation and signed-delivery flow in under 30 minutes without maintainer
help.

## Phase 9 — Production deployment guidance

**Purpose:** provide a safe reference deployment for the first real users.

- [ ] Support at least two stateless API replicas and two worker replicas.
- [ ] Document managed PostgreSQL HA, backup, restore, migration, and rollback.
- [ ] Provide Kubernetes/Helm examples only after Docker Compose remains simple.
- [ ] Provide ingress/WAF, egress proxy, DNS, secret-manager, and static outbound
      IP guidance.
- [ ] Separate API and worker database roles and pools.
- [ ] Add retention jobs and consider time partitioning only after measurements.
- [ ] Move large historical bodies to encrypted object storage only when needed.
- [ ] Run restore drills, rolling upgrades, worker drains, and migration checks.

**Completion gate:** a clean environment can be installed, upgraded, backed up,
restored, and rolled back using documented procedures.

## Phase 10 — Demand and public-launch validation

**Purpose:** verify adoption rather than treating stars as product validation.

- [ ] Interview 20 teams currently implementing outbound customer webhooks.
- [ ] Recruit at least five design partners and three non-critical production
      pilots.
- [ ] Record monthly deliveries, latency/failure profiles, current stack,
      security requirements, and switching blockers.
- [ ] Load test 50, 250, and 1,000 events/second with fan-out 1, 5, and 20 and
      receiver latency of 50 ms, 1 second, and timeout.
- [ ] Test a 10× ten-minute burst and declare the required drain window first.
- [ ] Test 50% endpoint failures for one tenant while monitoring healthy tenants.
- [ ] Publish measured limits and hardware/database assumptions.
- [ ] Track independent installations, active pilots, issue quality, and pilot
      retention for 90 days.

**Completion gate:** at least three teams operate real pilots, correctness and
isolation gates pass, and published capacity claims are backed by reproducible
tests.

## Broker adoption gate

Continue using PostgreSQL while it meets control-plane and queue-age SLOs at an
acceptable cost. Add a broker adapter only when measurements show one or more
of the following:

- [ ] Queue writes/claims/finalization materially damage control-plane latency.
- [ ] WAL, I/O, autovacuum, connection, or primary CPU limits repeatedly bind.
- [ ] Oldest-job-age SLOs fail after indexing, retention, batching, and safe
      worker scaling.
- [ ] Burst absorption must greatly exceed PostgreSQL write capacity.
- [ ] Customers require independent consumer groups, long replayable retention,
      strict partition ordering, or multi-region buffering.

When triggered, retain PostgreSQL as the control-plane source of truth. Write an
outbox row in the event transaction, publish through a relay, and keep consumers
idempotent because broker and relay delivery remain at least once.

## Initial priority order

1. Phase 0: PostgreSQL correctness and measurable baseline.
2. Phase 1: license, security, governance, and CI.
3. Phase 2: worker claim/lease correctness and immutable delivery snapshots.
4. Phase 3: production egress boundary and SSRF test corpus.
5. Phase 4: shared quotas, fair scheduling, and saturation protection.
6. Phase 5: retry budgets, circuit breaking, operational DLQ, and replay.
7. Phase 6: metrics, SLOs, dashboards, and bounded autoscaling.
8. Phase 7: tenant security lifecycle and audit.
9. Phase 8: SDKs, CLI, examples, and CloudEvents compatibility.
10. Phase 9: production deployment and recovery guidance.
11. Phase 10: design partners, pilots, burst tests, and public launch.
12. Broker adoption only after the explicit measurement gate is reached.

## Research references

- [PostgreSQL skip-locked queue behavior](https://www.postgresql.org/docs/current/sql-select.html)
- [GitHub webhook overview](https://docs.github.com/en/webhooks/about-webhooks)
- [Stripe webhook delivery recovery](https://docs.stripe.com/webhooks/process-undelivered-events)
- [Shopify webhook verification](https://shopify.dev/docs/apps/build/webhooks/verify-deliveries)
- [AWS retry behavior](https://docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html)
- [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Transactional outbox pattern](https://microservices.io/patterns/data/transactional-outbox.html)
- [Kubernetes horizontal autoscaling](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)
- [KEDA event-driven autoscaling](https://keda.sh/)
- [CloudEvents specification](https://github.com/cloudevents/spec)

Internet-derived material was paraphrased for compliance with licensing
restrictions.
