# Phase 8 developer adoption

## Current scope

Phase 8 adds a publishable Python package, raw-byte receiver helpers, an optional
SQLAlchemy outbox relay, `webhookctl`, native/CloudEvents wire documentation,
and minimal producer/receiver examples. The service remains one modular
codebase with PostgreSQL as its durable delivery queue.

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

Management commands read `WEBHOOK_PLATFORM_TOKEN`; test-event commands read
`WEBHOOK_PLATFORM_API_KEY`. Secrets are deliberately not accepted as command
arguments.

```bash
export WEBHOOK_PLATFORM_URL=http://localhost:8000
export WEBHOOK_PLATFORM_TOKEN='<bearer token>'
webhookctl projects list --organization <organization-id>
webhookctl projects create --organization <organization-id> --name Events
webhookctl endpoints create --project <project-id> --url https://receiver.example/webhooks
webhookctl deliveries list --project <project-id>
webhookctl deliveries attempts --project <project-id> --delivery <delivery-id>
webhookctl replay one --project <project-id> --delivery <delivery-id>
webhookctl replay bulk --project <project-id> --delivery <id-1> --delivery <id-2> --idempotency-key incident-42

export WEBHOOK_PLATFORM_API_KEY='<producer key>'
webhookctl events send --type test.event --idempotency-key test-42 --payload-file payload.json
```

Endpoint creation intentionally prints the one-time signing secret to standard
output. Capture it directly into a secret manager and avoid terminal/session
recording. Other errors contain only bounded status and error codes.

## External completion gate

Automated checks cannot satisfy the phase completion gate. Recruit ten
developers who did not implement the feature. On a clean machine, provide only
the public setup documentation and measure from start until a receiver has
durably accepted a correctly signed event. Record completion time, help needed,
failed step, operating system, Python version, and documentation feedback.

Phase 8 passes only when at least eight of ten finish in under 30 minutes without
maintainer help. Until then, package publication, the 30-minute target, minimal
portal decision, and the phase gate remain open; Phase 9 must not begin.
