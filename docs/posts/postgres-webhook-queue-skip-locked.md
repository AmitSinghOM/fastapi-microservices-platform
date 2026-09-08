# Postgres as a webhook queue: what SKIP LOCKED doesn't solve

*Draft — publish after the repository is public and the SDK is on PyPI.
Verify every claim against the launch commit before posting.*

---

Every "you don't need Kafka" thread ends the same way: someone posts
`SELECT ... FOR UPDATE SKIP LOCKED` and declares the problem solved. I
believed that too, so I built an entire webhook delivery platform on
PostgreSQL alone — no Redis, no broker — and then ran a million deliveries
through it with a correctness checker watching every row.

The one-liner is real: SKIP LOCKED is a genuinely great primitive. It is
also maybe 20% of a durable queue. This post is about the other 80% — the
five problems that showed up on the way to a million-job run with zero lost
work, and the one target I missed.

## What SKIP LOCKED actually gives you

One thing, and it gives it perfectly: concurrent workers can claim rows
without blocking each other or double-claiming inside the transaction.

```sql
SELECT id FROM deliveries
WHERE status = 'pending' AND next_attempt_at <= now()
ORDER BY next_attempt_at
FOR UPDATE SKIP LOCKED
LIMIT 50;
```

Everything after that query is where queues actually fail.

## Problem 1: your worker will die holding a claim

A row lock lives exactly as long as its transaction. But delivering a
webhook means making an HTTP call to someone else's server, and you must
never do network I/O inside a database transaction (problem 2). So you
commit the claim first — and now nothing protects the row if the worker
dies mid-delivery.

The fix is a lease: claiming stamps the row with a random token and an
expiry computed from **database time**, not worker clocks. A stalled
worker's rows become claimable again when the lease lapses. But that
creates a nastier race: the original worker may wake up *after* its lease
expired, finish the HTTP call, and try to record an outcome for a row
another worker now owns. Finalization therefore requires the current
`processing` state **and** the matching lease token, atomically:

```sql
UPDATE deliveries
SET status = 'succeeded', ...
WHERE id = $1 AND status = 'processing' AND lease_token = $2;
```

Zero rows updated means you lost the race; the stale worker's result is
discarded. In the million-job run, injected crashes produced exactly zero
accepted stale finalizations. This token guard, not SKIP LOCKED, is the
line that makes horizontal workers safe.

## Problem 2: HTTP does not belong in a transaction

Holding a transaction across a 30-second receiver timeout pins a
connection, blocks vacuum, and stretches your lock windows. So the
lifecycle is: short transaction to claim → commit → HTTP outside any
transaction → short transaction to finalize. The price is a crash window
between HTTP success and finalization: the receiver got the webhook, you
crashed, the lease expired, someone redelivers. That is not a bug — it is
the definition of at-least-once, and it is why receiver-side deduplication
by event ID is part of the wire contract rather than an afterthought. My
attempt numbers even have intentional gaps after crashes; pretending
otherwise would just hide the semantics from operators.

## Problem 3: SKIP LOCKED has no opinion about fairness

`ORDER BY next_attempt_at` is a starvation machine. One tenant enqueues
500k deliveries, and every other tenant's fresh work waits behind the
backlog. Nothing in Postgres solves this for you.

I ended up with a short global scheduler lock inside which the claimer
accounts for unexpired leases, then walks **persistent round-robin cursors**
over tenants and endpoints, respecting per-deployment, per-tenant, and
per-endpoint concurrency limits plus a shared endpoint token bucket — all
of it stored in Postgres so every API replica and worker sees one truth.
Retries additionally draw from a per-endpoint budget so a flapping receiver
can't monopolize attempts, and repeated failures open a circuit with
exactly one half-open probe across all workers. None of this is exotic; all
of it is state machines in ordinary rows. That's the point — the database
you already run can hold it.

## Problem 4: connection math is part of the queue design

Queues die by connection exhaustion more often than by throughput. API
pools × API replicas + worker pools × worker replicas must fit inside
`max_connections` with headroom, so the platform refuses to *start* if the
multiplied budgets don't fit. The million-job run peaked at **11**
PostgreSQL connections and generated 2.82 GiB of WAL — the queue is not
free, but it is boringly predictable.

## Problem 5 — the one I missed: queue age under backlog

Here is the honest part. My provisional SLO said the oldest runnable job
should be under 30 seconds at p95. Under the million-job single-runtime
run, queue age hit **p95 134.5 s**. The arithmetic is unforgiving: jobs
were generated at ~31k/s and drained at ~9.3k/s, so a deep backlog aged
before it drained. A single worker runtime cannot win that race, and no
amount of SKIP LOCKED changes it.

I'm keeping the SLO unmet on the books rather than quietly redefining it.
The path forward is measured, not vibes: bounded worker scaling against
drain-rate metrics first, and a broker only if an explicit trigger list is
ever hit (WAL/vacuum pressure binding, burst absorption beyond Postgres
write capacity, or customers needing replayable multi-consumer retention).
If that day comes, Postgres stays the source of truth and an outbox feeds
the broker — because the broker is also at-least-once, and the correctness
story above doesn't transfer to it for free.

## The receipts

1,000,000 expected, persisted, succeeded deliveries; 1,000,000 unique
attempt pairs; zero missing, zero unexplained duplicates, zero accepted
stale finalizations; reproduction command in the repo. A scripted
clean-machine gate goes from `git clone` to a verified signed delivery in
about 30 seconds, and it is the release gate, not a demo.

If you think these numbers are wrong, I'd genuinely like the issue — the
benchmarks page exists to start that argument. *(Repo link at launch.)*
