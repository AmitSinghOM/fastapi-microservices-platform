# Choosing a webhook delivery layer: where this project fits

An evaluator's page. It states what this project is, what it is not, and
how to think about it against the two categories it is usually compared
with: **hosted webhook services** (Svix and similar) and **self-hosted
gateways that require a message broker or cache alongside the database**
(Convoy and similar). Claims about this project are verifiable in this
repository and are linked; claims about other products are kept to their
publicly stated category and deployment model, because their feature sets
and pricing change and should be checked on their own sites at decision time.

## The one-sentence positioning

Self-hosted webhook delivery on **PostgreSQL alone** — no Redis, no Kafka —
with Standard Webhooks-compliant signatures, evidence-gated releases, and a
benchmarks page that reports the target it missed.

## Decision table

| Question | Hosted service | Self-hosted gateway + broker | This project |
| --- | --- | --- | --- |
| Where do payloads live? | Vendor infrastructure | Your infrastructure | Your infrastructure; nothing phones home |
| Stateful dependencies you operate | None (that is the product) | Database **and** Redis/Kafka/RabbitMQ | PostgreSQL only ([why](posts/postgres-webhook-queue-skip-locked.md)) |
| Receiver verification | Vendor SDKs, usually Standard Webhooks | Product-specific or Standard Webhooks | **Standard Webhooks** by default since 4.0; verified in CI by the official library ([ADR 0002](adr/0002-standard-webhooks-alignment.md)) |
| Delivery semantics | At-least-once | At-least-once | At-least-once, snapshotted immutable envelopes, lease-fenced finalization ([operations](operations.md)) |
| Multi-tenancy, RBAC, retention | Typically tiered by plan | Varies | Included: organizations/projects, role sets, per-tenant retention, quotas and fairness |
| Outbound network control | Vendor egress IPs | Yours | Yours: two-layer SSRF boundary with a CONNECT-only egress proxy ([egress](phase3-egress-boundary.md)); receivers can allowlist your egress IPs |
| Evidence you can rerun | Vendor status page | Varies | One-million-delivery zero-loss gate and a scripted clone-to-verified-delivery gate ([benchmarks](benchmarks.md)) |
| License / cost | Subscription | Usually open core | Apache-2.0, no paid tier |
| Operational maturity | High | Product-dependent | **Young**: single maintainer, single-region, queue-age SLO not yet met |

## When this project is the right choice

- You already run PostgreSQL and do not want to add a broker or cache for
  webhooks alone.
- Payloads must not leave your network, or you need to control egress IPs.
- Your receivers are (or can be) Standard Webhooks consumers, so switching
  delivery layers later costs nothing on the receiving side.
- You want to read the failure modes before you adopt: the threat model,
  release policy, and benchmarks are written for that reader.

## When it is not

- You need a vendor to be accountable for uptime, or a multi-region
  delivery plane, today.
- You need a white-label portal your customers log into to manage their own
  endpoints. Ours is an operator portal; a customer-facing one is deferred
  ([priority plan](../priority-Feature.md)).
- You need payload transformations at delivery time. Deliberately not
  built: it conflicts with the immutable-envelope guarantee.
- You need broker ingest (consume from Kafka/SQS and fan out as webhooks).
  The transactional outbox relay is the supported ingestion path instead.
- You need Kubernetes/Helm manifests now; the reference deployment is
  Docker Compose.

## Migration cost, both directions

- **Into this project:** producers call one HTTP endpoint with an
  idempotency key, or use the outbox relay; receivers keep their Standard
  Webhooks verifier and swap the secret.
- **Out of this project:** because the wire format is the Standard Webhooks
  spec, receivers do not change; only the producer integration moves. Data
  export is plain PostgreSQL.

## How to evaluate in an afternoon

1. [Quick start](quickstart.md): first verified delivery.
2. Break it: stop the worker mid-run, kill a receiver, send duplicates,
   replay a dead delivery from the portal. The guarantees page says what
   should happen.
3. Read [the benchmarks](benchmarks.md) including the missed target, then
   run the gate yourself: `python scripts/phase8_clean_machine_gate.py`.
4. Read [the threat model](threat-model.md) and decide whether the accepted
   risks are yours to accept.

## Honesty notes

- The one-million-delivery run measured correctness, not throughput
  ceilings; latency under backlog missed its provisional target
  (p95 134.5 s against 30 s). This page will be updated when a measured run
  meets it, not before.
- Feature and pricing specifics of other products are intentionally absent.
  If you need a side-by-side, take the left two columns above as prompts and
  fill them from each vendor's current documentation.
