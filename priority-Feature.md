# Priority and Feature Status

Status of the Svix/Convoy competitive comparison and the current priority
order for what the package actually needs. Grounded in the staff review of
2026-09-08, [the action plan](action.md), and
[ADR 0002](docs/adr/0002-standard-webhooks-alignment.md). The governing
rule is the action plan's own: complete phases in order and add
infrastructure only in response to measured need — the binding constraint
today is **release and adoption, not features**.

## Competitive feature scoreboard (vs Svix / Convoy / Hook0)

| # | Feature | Status | Notes |
| --- | --- | --- | --- |
| 1 | Event-type subscriptions + filtering | ✅ Done | Migration `0010`; exact + trailing `prefix.*` wildcards; filtered at acceptance before admission; SDK and `webhookctl` support. The review's #1 gap, closed. |
| 2 | Standard Webhooks alignment | ✅ Phase one done | ADR 0002 accepted and implemented: opt-in per-endpoint `standard` scheme (migration `0011`), emission cross-verified by the official `standardwebhooks` library, auto-detecting SDK receiver. 4.0 default flip planned, gated on PyPI (see below). |
| 3 | Multi-language verification SDKs | 🟡 Mooted, snippets missing | The `standard` scheme means any Standard Webhooks library (Python/JS/Go/Java/Ruby/PHP/Rust) verifies deliveries — no need to ship our own SDKs. Missing: short per-language verification snippets in the docs. |
| 4 | White-label embeddable consumer portal | ❌ Not built (deferred) | Svix's flagship: magic-link scoped tokens so a SaaS's customers self-manage endpoints. Our portal is operator-facing. Largest remaining product gap; build only on Phase 10 pilot evidence. |
| 5 | Payload transformations | ❌ Deliberately deferred | Conflicts with the immutable-envelope guarantee. If ever built: transform at dispatch, never mutate the accepted snapshot. |
| 6 | Broker ingest sources (Kafka/SQS/RabbitMQ) | ❌ Deliberately not building | The SQLAlchemy transactional outbox relay is the answer for the target user. Missing: one positioning paragraph saying so explicitly. |
| 7 | Static egress IP story | ❌ Doc gap only | The CONNECT proxy + NAT already provide it; no doc tells receivers they can firewall to the egress IPs. ~1 paragraph. |
| 8 | Kubernetes/Helm reference | ❌ Correctly deferred | Phase 9 item per the action plan's "Compose stays simple first" rule. |

Known small inconsistency: the portal endpoint form has the
`signature_scheme` select but no `event_types` field, so feature #1 is
creatable via API/SDK/CLI but not via the portal.

## Priority list (what the package needs now)

| Priority | Item | Why now | Effort / owner |
| --- | --- | --- | --- |
| P0 | Push the 13 local commits | All 2026-09-08 work (3 migrations, ADR 0002, security fixes) exists only on one machine's local `main`. Single biggest risk. | ~5 min; requires the owner's terminal (agent pushes to main are policy-blocked). |
| P1 | Signed release candidate → real PyPI | `d321e67` is CI-green but unsigned; SDK is TestPyPI-only. Unblocks three things at once: Phase 8 completion, receiver-first migration (receivers must be able to `pip install` the auto-detecting verifier), and the 4.0 default flip whose hard entry criterion is exactly this. Highest leverage per hour. | Owner: configure signing key, follow the release workflow. |
| P2 | Phase 8 usability gate | 8/10 independent developers completing clean-machine install + signed delivery in under 30 minutes. The only open gate automated tests cannot close. Recruiting has lead time — start in parallel. | Owner-driven; agent can draft the tester kit. |
| P3 | Adoption-friction docs | The items a Phase 8 tester hits in their first 30 minutes: per-language verification snippets (#3), static-IP paragraph (#7), outbox-as-ingest-answer positioning (#6), portal `event_types` field. | ~1 hour total; agent can do end-to-end. |
| P4 | Phase 9 production deployment guidance | Required before real pilots; not started. | Multi-day docs + reference-deployment work. |
| P5 | Queue-age SLO honesty | 30 s SLO unmet (p95 134.46 s at baseline). Before Phase 10 publishes capacity claims, either tune toward it or formally restate it. Cheap to restate, expensive to ignore. | Decision + either tuning work or a one-line SLO revision. |
| Parked | 4.0 default flip (ADR 0002 staging) | Sequenced behind P1 by design: flip only after the SDK is installable from PyPI and at least one real receiver has run `standard` end-to-end. Code change is trivial (Pydantic default only; leave DB `server_default` as `legacy`). Batch with the `EXAMPLE_ITEMS_ENABLED` off-default in one major release. | Blocked on P1. |
| Deferred | Feature work: white-label portal (#4), transformations (#5), broker ingest (#6), Helm (#8) | All wait for Phase 10 pilot evidence. Building Svix's flagship feature before having one design partner inverts the "measured bottlenecks first" rule. | Revisit after Phase 10 interviews/pilots. |

## Summary

The package is feature-complete **for its current phase**. What it needs
now is to ship (P0/P1), get verified by strangers (P2), and get used
(Phase 9/10). P0–P2 need the owner personally; P3 is delegable today.
