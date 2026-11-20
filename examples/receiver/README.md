# Receiver example

From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export WEBHOOK_SIGNING_SECRET='<one-time endpoint signing secret>'
uvicorn app:app --port 9000
```

The receiver verifies exact raw bytes before parsing and persists each signed
event ID and body in a SQLite inbox before returning `202`. Verified duplicates
return `204`. SQLite is only an adoption example; production deployments should
use their application database and commit the unique event ID with durable
acceptance or application changes.

The request body is read as a bounded stream (`WEBHOOK_RECEIVER_MAX_BODY_BYTES`,
default 1 MiB, matching the SDK's own `DEFAULT_MAX_BODY_BYTES`) before it is
ever joined into one buffer, so an oversized or chunked-forever body is
rejected with `413` while still streaming in, not after it has already been
fully allocated in memory. `verify_request` applies the same bound again as a
second, SDK-side guard, checked before HMAC verification or JSON parsing.

Platform staging/production targets require public HTTPS on port 443. Use a TLS
terminating tunnel or deployed HTTPS receiver for end-to-end platform testing;
plain local HTTP is development-only.

## Optional bearer check (defense in depth, off by default)

Set `WEBHOOK_RECEIVER_BEARER_TOKEN` to additionally require
`Authorization: Bearer <token>` before signature verification. This is
**additive, not a replacement** for signature checking, and it is **not**
something the platform itself satisfies: the delivery worker never sends an
Authorization header to receivers (there is no outgoing-header feature).
Enable it only if a trusted front door you control (gateway, sidecar, tunnel)
injects that header for traffic reaching this app — never for traffic
delivered directly by the platform, which will otherwise get `401`.

## Adapting to another framework

The verification call is framework-agnostic: pass the exact raw request
bytes and headers to `verify_request` (or `verify_and_claim` with a durable
deduplicator) before any JSON parsing, and treat `DuplicateEvent` as success.
A thin adapter for Flask/Django/etc. only needs to supply those two inputs
from its own request object and keep the same durable-insert-before-200
ordering shown in `accept_once` above.
