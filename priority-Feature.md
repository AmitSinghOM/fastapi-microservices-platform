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
| 3 | Multi-language verification SDKs | ✅ Mooted and documented | The `standard` scheme means any Standard Webhooks library verifies deliveries; the adoption guide carries Python, JavaScript, and Go snippets (added 2026-09-09) and `docs/comparison.md` states the ecosystem position. No bespoke SDKs planned. |
| 4 | White-label embeddable consumer portal | ❌ Not built (deferred) | Svix's flagship: magic-link scoped tokens so a SaaS's customers self-manage endpoints. Our portal is operator-facing. Largest remaining product gap; build only on Phase 10 pilot evidence. |
| 5 | Payload transformations | ❌ Deliberately deferred | Conflicts with the immutable-envelope guarantee. If ever built: transform at dispatch, never mutate the accepted snapshot. |
| 6 | Broker ingest sources (Kafka/SQS/RabbitMQ) | ❌ Deliberately not building | The SQLAlchemy transactional outbox relay is the answer for the target user. Missing: one positioning paragraph saying so explicitly. |
| 7 | Static egress IP story | ✅ Done 2026-09-09 | The README SSRF/egress section tells receivers they can allowlist the platform's egress IPs (CONNECT proxy + NAT). |
| 8 | Kubernetes/Helm reference | ❌ Correctly deferred | Phase 9 item per the action plan's "Compose stays simple first" rule. |

The portal endpoint form exposes both `signature_scheme` and `event_types`
(the earlier inconsistency was fixed on 2026-09-09).

## Priority list (what the package needs now)

| Priority | Item | Why now | Effort / owner |
| --- | --- | --- | --- |
| P0 | Push the local commits | ✅ **Done 2026-09-09/10.** All work is on `origin/main`; nothing local-only remains. | Done. |
| P1 | Signed release candidate → real PyPI | ✅ **Done; latest `0.1.2` on 2026-09-13.** `fastapi-microservices-platform-sdk` is on [PyPI](https://pypi.org/project/fastapi-microservices-platform-sdk/) (`0.1.1` 2026-09-11, `0.1.2` 2026-09-13), each promoted byte-identical from cooled TestPyPI artifacts of a signed tag. `0.1.2` closed the pipeline-hardening gate with live evidence: byte-reproducible wheel **and** sdist (independently rebuilt from the tag), hash-matched candidate restore proven on a real retry, no-rebuild guard, cooling-off enforced in code, PEP 639 license metadata. `0.1.0` was burned on TestPyPI and superseded. Unblocks receiver-first migration (ADR 0002) and the 4.0 default-flip entry criterion. | Done. |
| P2 | Phase 8 scripted clean-machine gate | ✅ **Done 2026-09-08.** `scripts/phase8_clean_machine_gate.py` runs the full adoption flow unattended from a fresh clone + venv — install, auth, org/project/key/endpoint (`standard` scheme), signed event, worker delivery to `succeeded`, exactly-once receiver acceptance, CLI inspection. Two consecutive green runs (33.8 s / 31.9 s vs 1,800 s budget). Required a new dev-only `ALLOW_PRIVATE_WEBHOOKS` flag, which also fixed the quick start never completing locally. | Done; evidence in `action.md`. |
| P3 | Adoption-friction docs | ✅ **Done 2026-09-08.** Standard Webhooks verification snippets (Python/JS/Go, APIs verified against the official library READMEs) in the adoption guide; static egress IP paragraph and outbox broker-ingest positioning in the README; portal endpoint form gained the `event_types` filter field. | Done. |
| P4 | Phase 9 production deployment guidance | Required before real pilots; not started. | Multi-day docs + reference-deployment work. |
| P5 | Queue-age SLO honesty | 30 s SLO unmet (p95 134.46 s at baseline). Before Phase 10 publishes capacity claims, either tune toward it or formally restate it. Cheap to restate, expensive to ignore. | Decision + either tuning work or a one-line SLO revision. |
| Done | 4.0 default flip (ADR 0002 staging) | ✅ **Implemented 2026-09-14 as platform 4.0.0.** Both entry criteria held: the SDK is on PyPI (`0.1.1`/`0.1.2`), and the scripted clean-machine gate runs a real receiver on `standard` end-to-end (endpoint created with `--signature-scheme standard`, example receiver verifies with the one-time secret, durable acceptance checked; passed 2026-09-08, 31.9 s and 33.8 s). Changes: `EndpointCreate`/service default → `standard`; DB `server_default` stays `legacy`; `/items` defaults off (`EXAMPLE_ITEMS_ENABLED=true` to keep); portal preselects `standard`; README leads with the standard scheme; release policy carries 4.0 upgrade notes. Regression tests lock the default and the untouched-legacy guarantee. `v3.0.0` is tagged at the pre-flip commit `9441248` as the last 3.x release; `v4.0.0` at the 4.0 commit. | Owner: tag `v3.0.0` then `v4.0.0`. |
| Deferred | Feature work: white-label portal (#4), transformations (#5), broker ingest (#6), Helm (#8) | All wait for Phase 10 pilot evidence. Building Svix's flagship feature before having one design partner inverts the "measured bottlenecks first" rule. | Revisit after Phase 10 interviews/pilots. |

## P1 release phases (owner-only runbook)

**Status: completed twice (`0.1.1` on 2026-09-11, `0.1.2` on 2026-09-13);
retained as the reference procedure for every future `sdk-v*` release.**
Evidence for each phase lives in `action.md`.

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
