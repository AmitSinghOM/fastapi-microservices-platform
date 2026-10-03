# Scoped adoption verification

This records local work after reliability commit `6f794c7`. Adoption changes
are uncommitted and were not pushed, published, deployed, or exercised against
live receivers. SDK 0.1.2 publication does not include these new APIs.

## Scope and review

- Reuse existing project API keys, digests, expiration, revocation and audits;
  no new database model, migration, broker, or delivery-engine rewrite.
- Machine route opt-in is explicit. Producer defaults remain `events:write`.
  Project matching precedes scope authorization; administrative routes remain
  human-only. Read and write/replay permissions remain independent.
- Independent backend security review found no blocking issues. Intentional
  risks are documented: endpoint-write controls all project destinations and
  signing rotation; delivery-read exposes retained receiver responses;
  authorization is per-request, not atomic revocation of in-flight mutations.
- Main review caught unsafe ambiguous onboarding retries and unbound local
  checkpoint reuse before completion. Examples now require operator
  reconciliation after uncertainty and bind state to trusted onboarding inputs.
- No generic control-plane abstraction or new runtime dependency was introduced.
  Existing authorization, audit, signing, idempotency and SDK utilities are reused.

## Executed application gates

| Gate | Result |
| --- | --- |
| Python 3.12 complete application suite | **267 passed**, including all **20 PostgreSQL** cases |
| Python 3.11.14 complete application suite | **258 passed**, including all **20 PostgreSQL** cases |
| Python 3.11 newly added receiver module | **9 passed** separately; added after collection of the 258-test gate |
| SDK, CLI and onboarding/recovery examples | **51 passed on Python 3.12**, **51 passed on Python 3.11** (standalone SDK configuration) |
| Repository Ruff lint / format | Passed; **164 Python files** formatted |
| SDK wheel and sdist | Built twice from separate scratch copies; normalized hashes byte-identical; Twine passed |
| Compilation / diff / documentation links | Passed |
| Configured mypy | **21 source files passed** |
| Additional CI harness mypy targets | **3 source files passed** |
| Docker-free egress harness | **8 passed** |
| Deployment lock | **57 hashed runtime pins**, direct versions match |
| Dependency consistency | `pip check` passed in both verification runtimes |
| Scratch PostgreSQL migration head/drift | `alembic upgrade head` and `alembic check` passed; no new upgrade operations |

Application tests include 26 scoped-machine API regressions, a real PostgreSQL
machine replay/audit/idempotency case, and 9 receiver integration cases covering
legacy/Standard signatures, tampering, additive bearer guards, and durable inbox
deduplication. Existing producer/JWT, locking, retention and tenant tests pass.

The first Python 3.11 attempt failed because simultaneous pytest processes shared
`./test.db`: one fixture dropped tables another process was using. The fixture
now uses a per-test temporary file/engine, preserves independent database
connections, and disposes engines. The green gates above include that fix; this
was not fixed by suppressing failures or increasing timeouts.

A helper initially used the stale project `.venv` (Starlette 0.41.3), reporting a
missing 422 status constant. Both verification runtimes use locked Starlette
1.7.0, where it exists. No compatibility band-aid was applied to production code;
refresh local environments from current requirements before testing.

All `make check` constituent gates were executed; application tests used the
stronger PostgreSQL-inclusive suite instead of rerunning the SQLite-only target.
Build verification here means reproducible SDK artifacts, not an application
container build. No version bump/tag/publication was performed; built 0.1.2-named
artifacts are local verification outputs, not replacements for released files.

Final SDK tests explicitly cover read-only recovery, warning acknowledgment,
failed provisioning responses, changed checkpoint bindings, and checkpoint or
secret-write failures that must block duplicate provisioning on rerun.

## Limitations and remaining external gates

- Docker is unavailable locally. Full application container build, Compose
  validation, actual Squid/TLS/network isolation, and hosted CI are **not
  verified**. Do not interpret local tests or an SDK wheel build as deployment
  certification.
- Configured mypy passes, but an optional broader authentication check reports
  two pre-existing SQLAlchemy `Column` typing errors in `app/api_key_usage.py`
  (lines 76 and 86). They were disclosed, not suppressed or refactored in this
  scoped change.
- The existing passlib `crypt` deprecation warning remains on Python 3.11/3.12.
- Onboarding is a single-operator, private-directory example, not a concurrent
  provisioning service. Local checkpoints do not revalidate remote resources.
- Runtime test environments are isolated under `~/.aki/tmp`; the scratch
  PostgreSQL cluster is local-only and uses random test schemas. Migration
  verification uses the dedicated scratch `webhook_migrations` database.
