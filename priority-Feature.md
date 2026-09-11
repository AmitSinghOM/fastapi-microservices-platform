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
| P0 | Push the local commits | ✅ **Done 2026-09-09/10.** All work is on `origin/main`; nothing local-only remains. | Done. |
| P1 | Signed release candidate → real PyPI | ✅ **Done 2026-09-11.** `fastapi-microservices-platform-sdk 0.1.1` is on [PyPI](https://pypi.org/project/fastapi-microservices-platform-sdk/), promoted byte-identical from the cooled TestPyPI artifacts of signed candidate `198ba60` (tag `sdk-v0.1.1`, production run `34636739009`). `0.1.0` was burned on TestPyPI by a verifier bug and superseded. Unblocks receiver-first migration (ADR 0002) and the 4.0 default-flip entry criterion. Pipeline hardening for `0.1.2` is planned in `action.md`. | Done. |
| P2 | Phase 8 scripted clean-machine gate | ✅ **Done 2026-09-08.** `scripts/phase8_clean_machine_gate.py` runs the full adoption flow unattended from a fresh clone + venv — install, auth, org/project/key/endpoint (`standard` scheme), signed event, worker delivery to `succeeded`, exactly-once receiver acceptance, CLI inspection. Two consecutive green runs (33.8 s / 31.9 s vs 1,800 s budget). Required a new dev-only `ALLOW_PRIVATE_WEBHOOKS` flag, which also fixed the quick start never completing locally. | Done; evidence in `action.md`. |
| P3 | Adoption-friction docs | ✅ **Done 2026-09-08.** Standard Webhooks verification snippets (Python/JS/Go, APIs verified against the official library READMEs) in the adoption guide; static egress IP paragraph and outbox broker-ingest positioning in the README; portal endpoint form gained the `event_types` filter field. | Done. |
| P4 | Phase 9 production deployment guidance | Required before real pilots; not started. | Multi-day docs + reference-deployment work. |
| P5 | Queue-age SLO honesty | 30 s SLO unmet (p95 134.46 s at baseline). Before Phase 10 publishes capacity claims, either tune toward it or formally restate it. Cheap to restate, expensive to ignore. | Decision + either tuning work or a one-line SLO revision. |
| Parked | 4.0 default flip (ADR 0002 staging) | Sequenced behind P1 by design: flip only after the SDK is installable from PyPI and at least one real receiver has run `standard` end-to-end. Code change is trivial (Pydantic default only; leave DB `server_default` as `legacy`). Batch with the `EXAMPLE_ITEMS_ENABLED` off-default in one major release. | Blocked on P1. |
| Deferred | Feature work: white-label portal (#4), transformations (#5), broker ingest (#6), Helm (#8) | All wait for Phase 10 pilot evidence. Building Svix's flagship feature before having one design partner inverts the "measured bottlenecks first" rule. | Revisit after Phase 10 interviews/pilots. |

## P1 release phases (owner-only runbook)

Grounded in [the SDK release checklist](docs/sdk-release-checklist.md),
[the external-gate runbook](docs/phase8-external-gates.md), and
[the release policy](docs/release-policy.md). Every phase is sequential;
any mismatch or changed candidate stops the release (fix forward, never
replace published files).

1. **Phase A — signing capability (one-time, ~15 min).** Generate an SSH
   signing key (`ssh-keygen -t ed25519 -f ~/.ssh/git_signing_ed25519`),
   add it to GitHub as a *Signing Key*, set repo-local
   `gpg.format ssh` + `user.signingkey`. Test a signed commit and signed
   annotated tag on a disposable branch until GitHub shows **Verified**,
   then enable repo-local `commit.gpgsign` / `tag.gpgsign`.
2. **Phase B — push (P0 prerequisite).** Push the accumulated local
   commits to `origin/main` from the owner's terminal.
3. **Phase C — frozen candidate.** The old candidate `d321e67` is stale
   and unsigned. Make one small **signed** commit on `main`; confirm the
   exact-SHA main CI run and an explicitly dispatched Container run are
   green on that SHA; freeze it. Any material change afterward requires a
   new signed candidate.
4. **Phase D — publisher plumbing (one-time, web UI).** On TestPyPI and
   PyPI: add a *pending trusted publisher* for
   `fastapi-microservices-platform-sdk` (repo + workflow file +
   environment names `testpypi`/`pypi`; OIDC only, no tokens). On GitHub:
   create the `testpypi` and `pypi` environments restricted to `sdk-v*`
   tags with a **24-hour wait timer on `pypi`**, plus `main`/`sdk-v*`
   protections (no force-push or deletion). Past evidence recorded that
   none of these exist yet.
5. **Phase E — TestPyPI.** Signed annotated `sdk-v0.1.0` tag on the
   unchanged frozen candidate (Verified). Dispatch
   `python-sdk-test-release.yml` from that tag with the tag as input.
   Verify retained wheel/sdist/`SHA256SUMS` and preflight per the
   checklist; clean-venv install from TestPyPI with
   `import webhook_platform_sdk` and `webhookctl --help` smoke checks.
   Then wait **24 hours** without moving the tag or candidate.
6. **Phase F — production.** Review evidence from a second authenticated
   device/session; dispatch `python-sdk-release.yml` from the same tag;
   confirm preflight, environment wait, OIDC, and post-publication
   polling; clean `pip install fastapi-microservices-platform-sdk` from
   PyPI and record the final URL and hashes.

Completing Phase F satisfies the 4.0 default-flip entry criterion and
makes receiver-first migration real (receivers can install the
auto-detecting verifier).

**Resolved 2026-09-08:** the release checklist's stale 8-of-10 human-study
requirement was replaced with the scripted clean-machine gate, so the
runbook and checklist now agree.

## Summary

The package is feature-complete **for its current phase**. What it needs
now is to ship (P0/P1), get verified by strangers (P2), and get used
(Phase 9/10). P0–P2 need the owner personally; P3 is delegable today.
