# Launch story drafts

Working drafts for the public launch. The spine of every version is the
same: **lead with evidence and honesty, not with a feature list** — the
feature table is what every competitor also has; the receipts are not.

Positioning one-liner:

> Self-hosted webhook delivery on PostgreSQL alone — no Redis, no Kafka —
> with a one-million-delivery zero-loss correctness gate and a benchmarks
> page that reports the target it missed.

## Show HN draft

**Title:** Show HN: A self-hosted webhook delivery platform that runs on
PostgreSQL alone

I built an open-source (Apache-2.0) alternative to hosted webhook services
like Svix and self-hosted gateways like Convoy, for teams that want durable
outbound webhooks without operating Redis or Kafka. PostgreSQL is the only
dependency: it is the queue (`FOR UPDATE SKIP LOCKED` claims with
lease-token-guarded finalization), the scheduler, the rate limiter, and the
audit log.

What I think is worth your click:

- **Receipts over claims.** Before touching queue behavior I ran a
  1,000,000-delivery correctness gate: zero lost jobs, zero unexplained
  duplicates, zero accepted stale finalizations, with the reproduction
  command in the repo. The same page reports the target I missed (queue-age
  p95 was 134 s against a provisional 30 s SLO). docs/benchmarks.md
- **Standard Webhooks compliant.** Endpoints can emit spec-exact
  signatures, cross-verified in CI by the official `standardwebhooks`
  library, so receivers verify with the same ecosystem libraries they may
  already use. The design ADR records the migration bridge I rejected —
  because it would have broken my own receiver SDK's parser.
- **The boring production things, free:** per-endpoint event-type
  subscriptions with wildcards, endpoint circuit breaking, shared
  PostgreSQL-backed quotas and tenant fairness, immutable delivery
  snapshots, replay with audit, retention policies, RBAC, OTel tracing, and
  a two-layer SSRF boundary (application DNS checks plus a CONNECT-only
  egress proxy).
- **Fast to try honestly:** a scripted clean-machine gate installs from a
  fresh clone and completes a signed delivery end-to-end; it runs in about
  30 seconds and is the release gate, not a demo.

It is at-least-once by design (receiver deduplication is documented, with a
receiver SDK and examples), single-maintainer, and not battle-tested at
SaaS scale — the benchmarks page is explicit about what is and is not
claimed. I would genuinely value adversarial benchmarks and SSRF-bypass
attempts.

## r/selfhosted draft

**Title:** I built a webhook delivery platform that needs only PostgreSQL —
no Redis, no Kafka [Apache-2.0]

If your app needs to send webhooks reliably (retries, signatures, replay,
dead-letter handling) the usual options are a hosted service or a
self-hosted gateway that wants Redis plus a database. I wanted the version
where the only stateful thing is the PostgreSQL you already run.

Highlights for this crowd: Docker Compose with a locked-down egress proxy
out of the box, no phoning home, payloads never leave your network,
per-tenant retention policies, Prometheus metrics and Grafana dashboards
included, and a docs page of reproducible benchmarks that includes the
number that missed its target. Free and Apache-2.0 — the features some
alternatives paywall (circuit breaking, RBAC, retention, OTel) are just in
it.

Honest caveats: at-least-once delivery (your receiver dedupes; SDK helper
included), young project, one maintainer, SQLite works for kicking the
tires but PostgreSQL is the real path.

## Publishing checklist

- [ ] P0 push and P1 PyPI release completed first (links must resolve).
- [ ] README leads with the one-liner and links docs/benchmarks.md.
- [ ] Strip or verify every claim in these drafts against the repo at
      launch commit (the count-every-claim rule).
- [ ] Post timing: weekday morning US time; answer comments same day.
- [ ] Newsletters that accept async submissions: Console, TLDR,
      Python Weekly, Postgres Weekly.
