# Changelog

All notable changes to the platform and its Python SDK. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[the release policy](docs/release-policy.md). The platform (`vMAJOR.MINOR.PATCH`)
and the SDK (`sdk-vMAJOR.MINOR.PATCH`) are versioned independently.

## Platform

### [Unreleased]

#### Added
- `docs/quickstart.md`: one path from clone to a signed, verified delivery.
- `docs/operations.md`: guarantees, non-guarantees, SLO status, capacity
  knobs, and alerting on one page.
- `docs/comparison.md`: where this project fits against hosted services and
  broker-backed gateways, including when not to choose it.
- `scripts/bootstrap_env.py` and `make bootstrap`: create `.env` and fill
  the three shared secrets once, idempotently (replaces the README
  one-liner that rotated keys when re-run).
- This changelog.

#### Changed
- README leads with a three-command quick start; the from-source path is
  collapsed. `.env.example` is tiered into Required / Common / Advanced
  with identical keys and values.

### [4.0.0] — 2026-09-14

Two defaults change; no stored data changes and no new migration.

#### Changed
- New endpoints default to the `standard` (Standard Webhooks) signature
  scheme. Endpoints created earlier keep their stored `legacy` scheme, and
  every accepted or replayed delivery signs per its acceptance-time
  snapshot. Pass `signature_scheme: "legacy"` explicitly for receivers that
  have not yet upgraded. (ADR 0002 phase two.)
- The tutorial `/items` API defaults off. Set `EXAMPLE_ITEMS_ENABLED=true`
  to keep it; removal no earlier than 5.0.
- The portal preselects `standard`; the README leads with the Standard
  Webhooks scheme.

#### Upgrade notes
See [4.0 upgrade notes](docs/release-policy.md#40-upgrade-notes).

### [3.0.0] — 2026-09-13 (first tagged release; last 3.x)

The complete signature-scheme feature with `legacy` as the default, so
operators can opt endpoints into `standard` one at a time before 4.0.

#### Added
- Per-endpoint signature scheme (`legacy` | `standard`) with a per-delivery
  snapshot; `standard` is spec-exact Standard Webhooks and is cross-verified
  in CI by the official `standardwebhooks` library. Migration `0011`.
  (ADR 0002 phase one.)
- Per-endpoint event-type subscriptions with trailing `prefix.*` wildcards.
  Migration `0010`.
- Shared, PostgreSQL-backed per-account login throttling; stale throttle
  rows are purged by the lifecycle job. Migration `0009`.
- Request body-size guard (413) and a `REGISTRATION_ENABLED` toggle.
- Development-only `ALLOW_PRIVATE_WEBHOOKS` flag for local receivers
  (refused outside development).
- Portal: event-type filters and signature-scheme selector on endpoint
  creation.
- Scripted clean-machine gate (`scripts/phase8_clean_machine_gate.py`):
  fresh clone to verified signed delivery, the release gate for adoption.

#### Fixed
- API-key authentication survived lookup-prefix collisions by verifying
  every candidate row instead of failing with 500.
- Concurrent first-failure race in login throttling.
- Email identity normalized (lowercased) at every entry point.

#### Security
- Postgres container image pinned by digest.
- Threat model: scheme-downgrade threat, lockout denial-of-service row,
  proxy-header and body-limit operational requirements.

#### Baseline
The platform before the scheme feature: PostgreSQL-native delivery with `FOR UPDATE
SKIP LOCKED` claims, lease-token finalization, immutable accepted-work
snapshots, tenant fairness, retries with circuit breaking, audited
dead-letter replay, retention policies, RBAC, OpenTelemetry, and the
two-layer SSRF egress boundary. Earlier history is in the commit log.

## Python SDK (`fastapi-microservices-platform-sdk`)

### [0.1.2] — 2026-09-13
#### Changed
- License metadata is the SPDX expression `Apache-2.0` (PEP 639); the index
  page no longer embeds the full license text.
- Release pipeline: source distribution is byte-reproducible (normalized
  tarball metadata) and CI proves it by building twice; retained release
  candidates are restored by hash and never rebuilt.

### [0.1.1] — 2026-09-11
First release on PyPI.
#### Added
- Synchronous producer with caller-supplied idempotency keys and no implicit
  retries; control-plane client; `webhookctl` CLI.
- Receiver verification that auto-detects `legacy` and `standard` schemes,
  accepts either secret serialization, and ignores unknown signature tokens
  (the prerequisite for receiver-first migration).
- SQLAlchemy transactional outbox relay (`[outbox]` extra).
- `create_endpoint`/`update_endpoint` support `event_types` and
  `signature_scheme`; `webhookctl endpoints create|update` expose both.
#### Fixed
- Legacy-form secrets are decoded correctly in standard-scheme verification.

### [0.1.0] — 2026-09-10 (TestPyPI only; superseded)
Published to TestPyPI but never promoted: the release workflow's
post-publish verification could not read its own distribution directory
after the publish action added attestation sidecars. Fixed forward as 0.1.1.

[Unreleased]: https://github.com/AmitSinghOM/fastapi-microservices-platform/compare/v4.0.0...main
[4.0.0]: https://github.com/AmitSinghOM/fastapi-microservices-platform/compare/v3.0.0...v4.0.0
[3.0.0]: https://github.com/AmitSinghOM/fastapi-microservices-platform/releases/tag/v3.0.0
[0.1.2]: https://pypi.org/project/fastapi-microservices-platform-sdk/0.1.2/
[0.1.1]: https://pypi.org/project/fastapi-microservices-platform-sdk/0.1.1/
[0.1.0]: https://test.pypi.org/project/fastapi-microservices-platform-sdk/0.1.0/
