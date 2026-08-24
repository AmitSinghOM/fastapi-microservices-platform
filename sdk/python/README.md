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

`webhookctl` reads management credentials from `WEBHOOK_PLATFORM_TOKEN` and
producer credentials from `WEBHOOK_PLATFORM_API_KEY`. It does not accept secrets
as command-line options, where they could be exposed by process listings.
