# Phase 8 developer adoption

## Current scope

Phase 8 adds the `fastapi-microservices-platform-sdk` distribution,
raw-byte receiver helpers, an optional SQLAlchemy outbox relay, `webhookctl`,
native/CloudEvents wire documentation, and minimal producer/receiver examples.
The service remains one modular codebase with PostgreSQL as its durable delivery
queue and does not require Redis or Kafka.

[ADR 0001](adr/0001-product-identity.md) retains **FastAPI Microservices
Platform** and the `fastapi-microservices-platform` repository. Distribution
`fastapi-microservices-platform-sdk` intentionally differs from import
`webhook_platform_sdk`, CLI `webhookctl`, and environment prefix
`WEBHOOK_PLATFORM_`.

The package has not yet been released to a package index. Install it from the
repository while the external usability gate remains open:

```bash
git clone <repository-url>
cd fastapi-microservices-platform
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ./sdk/python
```

## Producer flow

```python
from webhook_platform_sdk import Producer

with Producer("http://localhost:8000", "whk_...") as producer:
    producer.send_event(
        "order.created",
        {"order_id": "42"},
        "order-42-created",
        envelope_mode="native",
    )
```

The SDK has explicit phase timeouts, refuses redirects, and performs no implicit
retry. After an ambiguous failure, callers may retry only with the same stable
idempotency key. Exceptions omit credentials and raw response bodies.

Use `envelope_mode="cloudevents"` only when a receiver expects CloudEvents 1.0
structured JSON. Native remains the backward-compatible default.

## Receiver flow

Pass raw bytes and headers to `verify_request` before JSON handling. Use
`verify_and_claim` with a durable deduplicator when its reservation can be in
the same transaction as receiver acceptance. Treat a verified duplicate as
success and return 2xx. The receiver example persists a signed inbox row before
returning `202`.

```bash
cd examples/receiver
python -m pip install -r requirements.txt
export WEBHOOK_SIGNING_SECRET='the one-time endpoint secret'
uvicorn app:app --port 9000
```

Production endpoints must be public HTTPS targets behind the required network
boundary; local HTTP is development-only.

## Transactional outbox

Install `./sdk/python[outbox]`. In the transaction that changes application
state, call `enqueue_outbox(session, type, payload, idempotency_key=...)`. Do not
commit inside the helper. Run `OutboxRelay.run_once()` from a separate bounded
process or scheduled job.

The relay locks a bounded due set, records short ownership, commits, calls the
platform outside the database transaction, then finalizes only its own lease.
A crash after platform acceptance can resend. Stable idempotency makes that
at-least-once relay behavior safe without adding a broker. Transient transport,
`408`, `425`, `429`, `503`, and `5xx` failures use capped exponential backoff
with full jitter and a maximum attempt count; bounded `Retry-After` is honored.
Other 4xx responses become terminal outbox failures instead of hot-looping.

Production users should create `webhook_outbox` through their normal migration
system rather than calling `create_outbox_schema`. Monitor unsent row age,
attempt count, and relay errors without logging payloads or credentials.

## CLI

`webhookctl auth login` prompts for the password without accepting it on the
command line, then stores the bearer token in a mode-`0600` credential file
bound to the configured base URL. Set `WEBHOOK_PLATFORM_CREDENTIALS` to choose
a different path. `WEBHOOK_PLATFORM_TOKEN` overrides the file for ephemeral
sessions, and test-event commands continue to read
`WEBHOOK_PLATFORM_API_KEY`. Secrets are deliberately not accepted as command
arguments.

```bash
export WEBHOOK_PLATFORM_URL=http://localhost:8000
webhookctl auth register --email owner@example.com --name Owner
webhookctl auth login --email owner@example.com
webhookctl organizations create --name Example
webhookctl projects create --organization <organization-id> --name Events
webhookctl api-keys create --project <project-id> --name producer
webhookctl endpoints create --project <project-id> --url https://receiver.example/webhooks
webhookctl deliveries list --project <project-id>
webhookctl deliveries attempts --project <project-id> --delivery <delivery-id>
webhookctl replay one --project <project-id> --delivery <delivery-id>
webhookctl replay bulk --project <project-id> --delivery <id-1> --delivery <id-2> --idempotency-key incident-42

export WEBHOOK_PLATFORM_API_KEY='<producer key>'
webhookctl events send --type test.event --idempotency-key test-42 --payload-file payload.json
```

Endpoint and API-key creation intentionally print their one-time secrets to
standard output. Capture them directly into a secret manager and avoid
terminal/session recording. Other errors contain only bounded status and error
codes.

## Minimal operational portal

`PORTAL_ENABLED=true` serves a dependency-free same-origin interface at
`/portal`. It covers registration/login, organization and project setup,
producer-key and endpoint creation, test events, delivery inspection, and
single-delivery replay. Advanced membership, policy, retention, export, and bulk
operations intentionally remain API/CLI workflows.

The portal does not use cookies, `localStorage`, or `sessionStorage`. Its bearer
token exists only in JavaScript memory and is cleared by logout or page refresh.
Passwords and pasted producer keys are cleared after use. All content is rendered
with text nodes, assets are same-origin, and the document applies a strict CSP
without inline code or third-party origins. This reduces persistence and XSS
exposure but cannot protect credentials from a compromised browser, extension,
host, or same-origin application. Deploy the portal only behind trusted HTTPS;
set `PORTAL_ENABLED=false` when operators do not need it.

Creation results intentionally display one-time API keys and endpoint signing
secrets. Move each secret directly into a secret manager and clear the result
panel; avoid browser, terminal, and session recording during setup.

## Package publication preparation

Two manual workflows implement the solo-maintainer release path. Both accept
only a GitHub-verified signed `sdk-vMAJOR.MINOR.PATCH` annotated tag, resolve it
to an exact commit in `origin/main`, require successful main-push CI for that
SHA, verify package/tag equality, check both distributions, verify SHA-256
hashes, install the wheel in a clean virtual environment, and exercise
`webhook_platform_sdk` plus `webhookctl`. The TestPyPI workflow builds from the
clean tagged checkout with a fixed source timestamp, retains the exact wheel,
source archive, and `SHA256SUMS` as one GitHub artifact, and restores those bytes
on retry rather than rebuilding. It preflights absent or matching
partial/complete registry state, repairs only missing files, and polls until exactly the expected non-yanked
release exists with matching hashes and sizes.

Both workflows must be dispatched from the signed tag while receiving that same
tag as input so protected environments can enforce `sdk-v*` deployment refs.
At least 24 hours after successful TestPyPI publication,
`python-sdk-release.yml` selects the successful test workflow for the exact tag
and commit and retrieves its manifest. It verifies manifest/API/download hashes,
reported and downloaded sizes, yanked state, approved HTTPS host and port before
and after redirects, then applies the same retry-safe preflight and completion
checks to PyPI. Configure an additional 24-hour `pypi` environment wait timer
and restrict both environments to `sdk-v*` tags; those remote settings cannot
be created in workflow YAML.

Before first use, configure trusted publishers for distribution
`fastapi-microservices-platform-sdk`, this repository, the respective workflow,
and exact environment. Do not configure long-lived package-index tokens. Follow
the [release checklist](sdk-release-checklist.md) and review from a second
authenticated device or fresh session. Independent approval is not guaranteed,
and no second maintainer is required.

Publication is intentionally not automatic and has not been performed. This
section summarizes, rather than reproduces, the linked
[PyPI trusted publisher setup](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [publisher usage](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
guidance.

## External completion gate

Automated checks cannot satisfy the phase completion gate. Follow the ordered
[external-gate runbook](phase8-external-gates.md), then use
[the independent study protocol](phase8-usability-study.md) with ten developers
who did not implement the feature. The recorder enforces pseudonymous IDs,
timezone-aware durations, strict under-30-minute timing, no-help qualification,
and the 8/10 aggregate threshold without storing credentials or payloads.

Phase 8 passes only when at least eight of ten finish in under 30 minutes without
maintainer help. Until then, package publication, the 30-minute target, and the
phase gate remain open; Phase 9 must not begin.
