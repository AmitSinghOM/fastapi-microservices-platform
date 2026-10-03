# FastAPI Microservices Platform Python SDK

The `fastapi-microservices-platform-sdk` distribution provides a synchronous
producer, control-plane client, raw-byte receiver verification, a SQLAlchemy
transactional outbox, and `webhookctl`. Its Python import remains
`webhook_platform_sdk`. Python 3.11+ is required.

Released versions are available from
[PyPI](https://pypi.org/project/fastapi-microservices-platform-sdk/).
The new scoped machine-auth and recovery APIs below are **unreleased** and
not included in SDK 0.1.2. Install this checkout from `sdk/python`:

```bash
python -m pip install -e .
```

Use a matching server checkout. This change does not publish a package or tag.

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

`ManagementClient` takes either a JWT `token` (positional, as above) or a
machine `api_key` (keyword-only), never both. A machine key scoped to
`endpoints:read`/`endpoints:write`/`deliveries:read`/`deliveries:replay` can
access only the explicitly opted-in project routes; credential and tenant
administration remain human-JWT-only. A human-authenticated client mints one
with `create_api_key(..., scopes=...)` (omit `scopes` for the unchanged `events:write`
default). `get_delivery_recovery(project_id, delivery_id)` fetches a
read-only, advisory replay-eligibility check -- eligibility is rechecked by
the replay call itself. See `docs/adoption-kit.md` for the full scope and
recovery contract, and `examples/onboarding` and `examples/recovery` for
runnable scripts.
