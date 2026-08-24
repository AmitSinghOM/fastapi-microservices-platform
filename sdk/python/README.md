# Webhook Platform Python SDK

The package provides a synchronous producer, control-plane client, raw-byte
receiver verification, a SQLAlchemy transactional outbox, and `webhookctl`.
Python 3.11+ is required.

```bash
python -m pip install .
```

```python
from webhook_platform_sdk import Producer

with Producer("http://localhost:8000", "whk_...") as producer:
    event = producer.send_event(
        "order.created",
        {"order_id": "42"},
        "order-42-created",
    )
```

The producer performs no implicit HTTP retries. If the result is ambiguous,
retry with the same idempotency key. `ApiError` exposes only status, bounded
error code, and bounded `Retry-After`; it never embeds raw response content.

Receiver code must pass the exact unparsed bytes to `verify_request` or
`verify_and_claim`. The latter accepts a durable `claim(event_id)` implementation.
Reserve the ID in the same transaction as durable receiver acceptance or state
changes, and return 2xx for a verified duplicate.

Install `.[outbox]` for `enqueue_outbox` and `OutboxRelay`. Enqueue in the
application transaction; the relay claims a bounded batch, commits, sends
outside the transaction, and finalizes with a lease token. A crash can resend,
so the relay always retains the same platform idempotency key.

`webhookctl auth register` and `webhookctl auth login` prompt for passwords
without placing them in process arguments. Login stores the bearer token in a
mode-`0600` file bound to the service base URL; `WEBHOOK_PLATFORM_TOKEN` can
override it for ephemeral environments. Organization, project, API-key, and
endpoint commands cover initial setup. Producer credentials come only from
`WEBHOOK_PLATFORM_API_KEY`. One-time API keys and endpoint secrets are printed
only in their explicit creation responses and should be captured directly into
a secret manager.
