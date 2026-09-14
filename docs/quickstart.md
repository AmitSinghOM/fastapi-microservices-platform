# Quick start: first verified webhook

One path, from a fresh clone to a delivery that your own receiver has
cryptographically verified. Every command and flag below is taken from the
tools' `--help` output at the commit this page ships with.

Two ways to run the platform:

| Path | Best for | Local receiver on your laptop? |
| --- | --- | --- |
| **From source** (this page) | First look, development, the example receiver | Yes, with the development-only `ALLOW_PRIVATE_WEBHOOKS=true` |
| **Docker Compose** | Realistic topology: PostgreSQL 17, migration job, API, worker, egress proxy | No — the egress proxy denies private address ranges by design (that is the SSRF boundary working). Point endpoints at a public URL or a tunnel. |

The platform's default posture refuses deliveries to private networks. The
flag used below is accepted only when `ENVIRONMENT=development`; it is not a
production setting.

## 0. Prerequisites

Python 3.11+ and a terminal. PostgreSQL is not required for this page: the
from-source path uses SQLite. (Production is PostgreSQL; see
[the release policy](release-policy.md).)

## 1. Install and configure (one command each)

```bash
git clone https://github.com/AmitSinghOM/fastapi-microservices-platform.git
cd fastapi-microservices-platform
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/bootstrap_env.py
```

`bootstrap_env.py` copies `.env.example` to `.env` and fills the three
shared secrets — `SECRET_KEY`, `API_KEY_PEPPER`, `WEBHOOK_SIGNING_KEY` —
only if they are empty. Running it again changes nothing. (The API and the
worker are separate processes; they must share these values or signatures
will never verify.)

For this walkthrough only, allow deliveries to your own machine:

```bash
printf 'ENVIRONMENT=development\nALLOW_PRIVATE_WEBHOOKS=true\n' >> .env
```

## 2. Start the API and the worker (two terminals)

```bash
uvicorn app.main:app
```

```bash
source .venv/bin/activate && python -m app.worker
```

Readiness: `curl -s localhost:8000/readyz`. The web UI is at
<http://localhost:8000/portal>; everything below can be done there instead
of with the CLI.

## 3. Install the SDK and CLI

```bash
pip install fastapi-microservices-platform-sdk
export WEBHOOK_PLATFORM_URL=http://localhost:8000
```

## 4. Create an account, project, and producer key

```bash
webhookctl auth register --email you@example.com --name "You"
webhookctl auth login --email you@example.com
webhookctl organizations create --name "Pilot"
webhookctl projects create --organization <organization-id> --name "orders"
webhookctl api-keys create --project <project-id> --name "local-producer"
```

Copy the API key from the last response — it is shown once — and export it
for the producer commands:

```bash
export WEBHOOK_PLATFORM_API_KEY=whk_...
```

## 5. Create an endpoint and start the example receiver

```bash
webhookctl endpoints create --project <project-id> \
  --url http://127.0.0.1:9000/webhooks \
  --event-type 'order.*'
```

The response includes `signing_secret`, shown once. It is a Standard
Webhooks secret (`whsec_…`, the 4.0 default), so any Standard Webhooks
library verifies it; the bundled receiver uses this project's SDK. In a
third terminal:

```bash
cd examples/receiver
python -m pip install -r requirements.txt
export WEBHOOK_SIGNING_SECRET='whsec_...'
uvicorn app:app --port 9000
```

## 6. Send an event and watch it arrive

```bash
webhookctl events send --type order.created --idempotency-key order-42-created
webhookctl deliveries list --project <project-id>
```

The delivery row reaches `succeeded`; the receiver's log shows the event
accepted once. Send the same command again: the idempotency key makes it a
no-op, and the receiver still has exactly one accepted event.

## What you just exercised

- HMAC-signed delivery with a per-endpoint secret, verified over the exact
  body bytes.
- Per-endpoint event-type subscription (`order.*`).
- Producer idempotency and receiver-side deduplication (the example receiver
  verifies with `verify_request`, then durably records the event ID so a
  redelivery is accepted once).
- The SSRF boundary (you had to opt in to reach your own laptop).

## Where next

- [Adoption guide](phase8-adoption.md): SDK, outbox relay, verification
  snippets in Python, JavaScript, and Go.
- [Wire protocol](wire-protocol.md): exact header formats and golden vectors.
- [Benchmarks](benchmarks.md): the one-million-delivery gate and the target
  it missed.
- [Operations](operations.md): guarantees, non-guarantees, what to alert on.
- Docker Compose: `docker compose up --build` starts PostgreSQL, the
  migration job, the API on `:8000`, the egress proxy, and the worker.
  Provide the three secrets from step 1 as environment variables.

<!-- Measured wall-clock time for this page, fresh clone to step 6, is
recorded in action.md when measured; do not state a number here that has
not been measured. -->
