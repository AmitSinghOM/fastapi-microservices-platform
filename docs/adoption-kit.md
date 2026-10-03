# Scoped adoption kit (unreleased)

This source-checkout kit adds scoped automation around the existing PostgreSQL
queue. It introduces no broker, transformation service, or schema migration.
Install the SDK from this checkout: `python -m pip install -e ./sdk/python`.
The new machine-auth and recovery helpers are **not in published SDK 0.1.2**.
Use the matching server checkout; no package publication is implied.

## Tenant mapping and onboarding

Keep an application-owned mapping `(customer_id, environment) -> project_id`.
Resolve it from your authenticated application context, never an event payload
or a caller-supplied project ID. Use a distinct project for each customer and
environment. Event-type filters are subscriptions, not authorization boundaries.
Projects in one organization still share organization quotas; use separate
organizations when separate organization-level quota/lifecycle policy is needed.

Run [the onboarding example](../examples/onboarding/app.py) from the repository
root with these trusted environment inputs:

- `WEBHOOK_PLATFORM_URL`: your server's base URL (HTTPS outside local development).
- `ONBOARDING_JWT_TOKEN`: authorized human provisioning token.
- `ONBOARDING_ORGANIZATION_ID`: an existing organization.
- `ONBOARDING_PROJECT_NAME`: a name identifying the customer and environment.
- `ONBOARDING_ENDPOINT_URL`: the intended public HTTPS receiver.
- `ONBOARDING_STATE_DIR`: a private, per-customer/environment directory **outside
  the checkout**, with no other writers. Never commit its files.

```bash
python examples/onboarding/app.py
```

Use a filesystem supporting file and directory fsync; keep the directory private
and provision its parent before running. Do not use shared or untrusted paths.

It creates a project, an endpoint, a producer-only key, and a recovery key with
`deliveries:read` plus `deliveries:replay`. Checkpoint IDs go into your trusted
mapping; plaintext credentials go into separate create-only mode-0600 files.
Import them into your secret manager, protect the directory, and remove local
secret copies under your retention policy. The checkpoint is not a secret store.

Successful steps can be skipped on rerun with the same inputs. A pending marker
means the request **may already have committed**: reconcile server state and
one-time secrets before changing that marker/checkpoint. Do not blindly retry,
remove a marker, or use a fresh directory to bypass uncertainty. Provisioning
mutations have no server-side idempotency guarantee. This is a single-operator
example, not a concurrent provisioning controller. It does not verify that
previously checkpointed resources remain present or that keys remain valid.

## Credential contract

Create keys using a human JWT via `POST /v1/projects/{project_id}/api-keys` or
`ManagementClient(base_url, token).create_api_key(..., scopes=[...])`. Select a
bounded `expires_in_days`, store the returned plaintext once, and rehearse
rotation/revocation. Existing keys and omitted scopes remain `events:write`.

All project routes below are relative to `/v1/projects/{project_id}`:

| Scope | Machine-enabled operations |
| --- | --- |
| `events:write` | `POST /v1/events`, bound to the key's project |
| `endpoints:read` | `GET /endpoints` |
| `endpoints:write` | `POST /endpoints`, `PATCH`/`DELETE /endpoints/{id}`, `POST /endpoints/{id}/rotate-secret` |
| `deliveries:read` | `GET /deliveries`, `/deliveries/{id}`, `/dead-deliveries`, `/deliveries/{id}/recovery` |
| `deliveries:replay` | `POST /replays`, with `Idempotency-Key` (also for one delivery) |

Write/replay scopes do not imply read. Endpoint write controls **all endpoints
in the project**, including destinations and signing rotation. Delivery read
includes retained attempt response bodies, but not event payload access; treat
receiver responses as potentially sensitive. Replay-only keys can replay known
IDs without inspecting them. Give diagnostic recovery automation both read and
replay, not endpoint write or event ingestion unless needed.

Use `ManagementClient(base_url, api_key=machine_key)` for automation. `token`
and `api_key` are mutually exclusive. Mixed `Authorization` and `X-API-Key`
headers fail closed on opted-in routes. Machine-only requests cannot administer
keys, projects, organizations, memberships, exports, event reads, pause/resume,
cancel/purge, or legacy single-delivery replay. These remain human-JWT operations.
The CLI still uses human JWTs for management; `api-keys create --scope` is
repeatable and `deliveries recovery` reads diagnostics.

Machine mutations atomically record `actor_type=api_key` and the credential's
public ID in immutable audit metadata, with no human actor ID or credential
material. Revocation stops subsequent requests, not already-authorized work.
Authentication usage timestamps include subsequently denied requests.

## Recovery without changing historical delivery

Set `WEBHOOK_PLATFORM_URL`, `RECOVERY_PROJECT_ID`, `RECOVERY_MACHINE_API_KEY`,
and `RECOVERY_DELIVERY_ID` from trusted operational context, then run:

```bash
python examples/recovery/app.py
```

The default is read-only. `--execute` requests an idempotent replay; warnings
additionally require `--acknowledge-warnings`. The deterministic idempotency key
prevents a retry of this example from creating another replay operation.

| Assessment | Operator action |
| --- | --- |
| `delivery_not_dead` | Inspect current state; this batch route only replays dead deliveries |
| `payload_purged` | Recover/reconcile through the business source of truth; payload cannot be replayed |
| `signing_version_unavailable` / `signing_version_retired` | Investigate historical signing compatibility; never silently substitute a current key |
| `endpoint_configuration_changed` | Replay keeps the old URL/key/scheme snapshot, **not** the edited endpoint |
| `endpoint_inactive` | Inspect current and snapshotted active state; replay does not rewrite either |
| `endpoint_paused` / `circuit_open` | Human operator investigates and uses existing runtime controls |

Eligibility is advisory, not a reservation; replay rechecks permissions,
retention, signing, lifecycle, and admission limits. It preserves the original
business event ID and delivery snapshot. A receiver that already processed the
event should accept its duplicate without repeating business effects. Do not
mint another business-event ID merely to evade deduplication. See
[delivery semantics](delivery-semantics.md) for guarantees and limits.

## Receiver compatibility and pilot checks

The [receiver example](../examples/receiver/README.md) verifies original raw
bytes using legacy or Standard Webhooks signatures and durably inserts an inbox
row before acknowledging. Verified duplicates return success. Framework adapters
must retain that ordering; commit business effects atomically with deduplication,
or process a durable inbox transactionally. There is no global delivery order.

The optional bearer guard is additive, **off by default**, and requires a
trusted ingress you own to insert its header: the worker does not send outbound
Authorization headers. Do not enable it for direct platform deliveries.

Before a pilot, verify two customers with identical event types remain isolated;
exercise expired/revoked/missing-scope keys; test duplicate and tampered receiver
requests; rehearse URL/key changes, retention expiry, and read-only recovery.
Run the Docker egress gate on the intended deployment topology. Local ASGI,
MockTransport, and PostgreSQL tests do not prove TLS/proxy/network isolation.
Use [operations](operations.md) for provisional alerts, not a claimed SLO.
