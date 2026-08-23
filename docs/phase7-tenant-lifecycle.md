# Phase 7 tenant security and lifecycle

## Authorization contract

Organization roles are `owner`, `admin`, and `member`. Owners control deletion
and all tenant operations. Administrators control members, policy, projects,
credentials, endpoints, replay, and audit, but cannot delete the organization.
Members can see the organization and require an explicit project role.

Project roles are `admin`, `operator`, and `viewer`. Project administrators
manage project settings, keys, endpoints, operations, and memberships.
Operators can read, replay, cancel delivery, and pause/resume/recover endpoints.
Viewers are read-only. Unknown tenants and projects return `404`; a visible
resource with insufficient permission returns `403`. Existing pre-Phase 7
`member` rows migrate to `admin` so upgrades do not silently remove access.
Deletion-pending organizations reject mutations and API-key ingestion.

## Credential lifecycle

Producer API keys have explicit scopes and expiry. `events:write` is the only
currently supported scope. Rotation creates a new one-time plaintext key in the
same family and shortens the old key's expiry to a bounded overlap deadline.
Revoked, expired, unscoped, malformed, inactive-project, and deletion-pending
credentials receive the same non-enumerating authentication failure.

Endpoint signing secrets remain deterministically derived and are never stored.
Each version has an activation time and optional retirement time. Rotation
activates a new version and keeps the previous version valid for at least the
maximum delivery age plus finalization margin. Accepted deliveries and replay
retain their snapshotted version; replay is rejected after that version retires
or after the event payload has been purged.

## Immutable audit history

Administrative mutations append a sanitized audit row in the same transaction.
PostgreSQL rejects audit-row update and delete operations with a trigger; ORM
schema creation installs the same protection for local integration tests.
Audit rows intentionally use public-ID and actor snapshots without cascading
foreign keys so organization deletion does not erase history.

Audit metadata is bounded and rejects credential-, destination-, payload-, and
response-related keys or suspicious credential values. It never stores API-key
plaintext/digests, signing secrets, authorization headers, destination URLs,
payloads, or response bodies.

## Retention, export, and deletion

Each organization has a persisted plan and independent payload and response
retention periods. The existing worker periodically clears expired response
content and clears event payload/canonical bytes only when every delivery is
terminal. Metadata, hashes, attempts, outcomes, and audit history remain. This
is an explicit retention erasure exception to append-only attempt semantics.

Organization export is bounded and contains safe organization, membership,
project, key metadata, endpoint metadata, policy, lifecycle, count, and audit
records. It omits endpoint URLs, credential material, payloads, and responses.

Deletion is owner-only, idempotent, and delayed by a configurable grace period.
During the grace period, authorized users may inspect status/export and an owner
may cancel. The worker claims due operations, cancels only reclaimable work,
waits for live leases, and deletes deliveries and orphan events in bounded
transactions before removing the organization. Competing PostgreSQL workers use
row locking with skip-locked claims. The final deletion audit remains after all
tenant rows are gone.

## Encryption and key management guidance

Use encrypted PostgreSQL storage and encrypted, access-controlled backups.
Require TLS for database, API, monitoring, and OTLP connections outside a
single-host development environment. Keep `SECRET_KEY`, `API_KEY_PEPPER`, and
`WEBHOOK_SIGNING_KEY` in a managed secret service backed by KMS/HSM controls;
do not place them in images, source, Compose files, telemetry, or audit data.
Grant runtime identities read access only to the exact secrets they need.

Root-key and pepper rotation requires a staged migration because existing JWTs,
API-key digests, and derived endpoint versions depend on current material. Use
versioned secret references, dual-read/controlled rehash windows, monitored
rollout, and a tested rollback; never replace roots in place without migration.
Restrict backup restore and key administration to separate least-privilege
roles, log access, rotate credentials after suspected exposure, and test restore
procedures regularly. The platform deliberately does not implement bespoke
field encryption; deployments needing payload-level encryption should use a
reviewed envelope-encryption design and external key custody.

## Validation evidence

Focused tests cover tenant hiding versus permission denial, role boundaries,
last-owner safety, API-key expiry/rotation/revocation, endpoint version overlap,
replay retirement and purge boundaries, sanitized audit coverage, safe bounded
export, policy retention, deletion blocking/cancel, worker retention cleanup,
bounded organization deletion, and PostgreSQL audit immutability. Migration
`0007_phase7_tenant_lifecycle` upgrades a clean PostgreSQL database, reports no
Alembic drift, downgrades to `0006`, and re-upgrades successfully. Downgrade is
refused after destructive retention erasure or while deletion is pending.