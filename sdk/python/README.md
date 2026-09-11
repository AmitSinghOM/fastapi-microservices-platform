# FastAPI Microservices Platform Python SDK

The `fastapi-microservices-platform-sdk` distribution provides a synchronous
producer, control-plane client, raw-byte receiver verification, a SQLAlchemy
transactional outbox, and `webhookctl`. Its Python import remains
`webhook_platform_sdk`. Python 3.11+ is required.

```bash
python -m pip install fastapi-microservices-platform-sdk
```

Releases are published to [PyPI](https://pypi.org/project/fastapi-microservices-platform-sdk/)
from signed `sdk-v*` tags through trusted publishing. To work on the SDK
itself, install from a checkout instead: `python -m pip install -e .`.

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
`WEBHOOK_PLATFORM_API_KEY`. Capture one-time API keys and endpoint secrets
directly into a secret manager.

Endpoints subscribe to every event unless created with an event-type filter.
`webhookctl endpoints create --event-type order.* --event-type user.created`
restricts fan-out to exact types and trailing `prefix.*` wildcards; the field
is omitted from the request when no filter is given, so the command also works
against servers that predate subscription filtering. `webhookctl endpoints
update` changes the URL, description, or active state, replaces the
subscription list with repeated `--event-type` flags, or clears it with
`--all-events`. The same operations are available programmatically through
`ManagementClient.create_endpoint(..., event_types=...)` and
`ManagementClient.update_endpoint`, where passing `event_types=None`
explicitly clears the filter.
