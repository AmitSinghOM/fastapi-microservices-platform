# Phase 2 Worker Safety Evidence

Phase 2 keeps PostgreSQL as the durable queue and preserves the single modular
codebase with separately scalable API and worker processes.

## Ownership and execution

- A worker claims at most its currently free execution slots. No leased row
  waits behind a local execution semaphore.
- PostgreSQL server time controls claim, heartbeat, expiry, and finalization.
  SQLite uses an aware local clock only because its current timestamp has
  whole-second precision and SQLite is not a concurrent production queue.
- Heartbeats renew only a live matching token. Finalization requires the same
  token and an unexpired lease in the state-transition transaction.
- The configured lease must exceed the overall attempt deadline, one heartbeat
  delay, and the finalization margin. Slot-aware claiming removes local queue
  wait from this equation.
- Shutdown stops claims, drains active work for a bounded grace period, releases
  canceled claims, and then closes HTTP and worker database resources.

## Immutable accepted work

Event acceptance stores exact canonical envelope bytes. Each delivery stores
its endpoint URL, active state, public ID, and signing-secret version in the same
transaction. Endpoint edits, deactivation, rotation, and later JSON
serialization cannot change accepted work. Explicit replay copies the original
snapshot; creating a new event is required to use new endpoint configuration.

Migration `0003_phase2_delivery_safety` adds and backfills these fields before
making them non-null. Historical endpoint values that changed before migration
cannot be recovered; existing rows receive the endpoint state visible during
the upgrade. Operators should drain or pause delivery changes during upgrade if
that distinction matters.

## Resource and hot-path bounds

API and worker engines use separate PostgreSQL pool size, overflow, and wait
budgets. API-key `last_used_at` writes are coalesced by key and flushed
periodically or at graceful API shutdown, so event ingestion has no usage-only
commit. The timestamp is intentionally eventually consistent.

## Validation — 2026-08-18

- 71 deterministic fast tests passed.
- 76 complete tests passed, including five live PostgreSQL concurrency tests.
- PostgreSQL tests cover competing claims, heartbeat ownership, concurrent
  finalization, expired-owner rejection, and concurrent idempotent fan-out.
- A populated disposable PostgreSQL database upgraded from revision `0002` to
  `0003`; canonical bytes and endpoint snapshots backfilled correctly.
- `alembic check` reported no model drift.
- Ruff, configured mypy checks, compilation, and `make check` passed.
- The existing Passlib warning about Python's deprecated `crypt` module remains.
