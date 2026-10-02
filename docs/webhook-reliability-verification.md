# Webhook reliability hardening verification

These results record local verification before the approved GitHub push. No
deployment, package publication, live receiver probe, or external CI dispatch
was performed for these results. Push-triggered CI results are separate.

## Changes and regression coverage

| Batch | Change | Evidence source |
| --- | --- | --- |
| 1 | Recheck due state in the locked candidate query; use PostgreSQL wall clock and post-lock lease checks | `test_worker_locking.py`, `test_postgres_concurrency.py`, `test_delivery_contracts.py` |
| 2 | Capture capped raw response bytes without decompression; classify observed stream failures as failed attempts, even after 2xx | `test_worker_responses.py`, `test_phase2_safety.py` |
| 3 | Check snapshotted signing version within the overall attempt deadline and stop at its retirement; preserve tenant boundaries | `test_worker_responses.py`, `test_tenant_delivery_isolation.py`, `test_signature_scheme.py` |
| 4 | Expand CI runtime/security coverage and replace public-host egress probes with isolated DNS/TLS fixtures | `tests/egress/`, `.github/workflows/ci.yml`, `.github/workflows/container.yml` |
| 5 | Advisory-led dependency upgrades, hashed runtime resolution and updater coverage | Requirements manifests and dependency-upgrade documentation |

## Verification boundaries

- SQLite and HTTPX MockTransport do not prove PostgreSQL locking or a deployed
  network boundary. Locking tests use independent transactions in a dedicated
  PostgreSQL 17 scratch cluster, with random per-test schemas.
- The container egress proof requires Docker, which is unavailable on this
  machine. Its source and fixture tests can be checked locally, but Compose
  validation, container builds, Squid behavior and network isolation remain
  **unverified** until that gate runs on a Docker-capable host.
- The signing deadline is conservatively anchored before the DB clock lookup;
  DB latency cannot extend validity. It is not an atomic receiver-side revocation
  mechanism: a request already sent cannot be retracted.
- Response diagnostics stop at the configured byte cap, including no reads for
  a zero cap. Errors after capture stops cannot be observed. Receivers still
  require durable event deduplication.
- Missing historical signing-version metadata fails closed. Migration 0007
  backfills the endpoint's then-current version, not every historical rotation.
  Before deployment, inspect pending/retry deliveries on pre-0007 snapshots;
  do not synthesize unverifiable key validity or silently switch keys.
- Password hashing and image upgrades must not be treated as routine manifest
  bumps. The existing passlib/bcrypt compatibility pair is intentionally held;
  preserving existing-password verification takes precedence over newest-version
  coverage. Container/base-image advisory coverage is not provided by pip-audit.

## Final executed results

The integrated application gate ran locally with an isolated upgraded Python
3.12 environment, before the 11 deployment-lock tests were added:

- Complete application suite, including PostgreSQL: **220 passed**. This
  includes **19 PostgreSQL cases**, not skipped concurrency tests.
- SDK tests: **32 passed**; Docker-free DNS/TLS fixture tests: **8 passed**.
- Ruff check/format: passed (152 Python files at this gate).
- Configured mypy: **21 files passed**; additional harness targets:
  **3 files passed**.
- SDK wheel and sdist: built successfully; `twine check` passed both.
- `alembic upgrade head` and `alembic check`: passed on scratch PostgreSQL;
  no schema drift. All 11 migrations had also passed on a fresh scratch DB
  before upgrading dependencies.
- `compileall`, `pip check`, YAML parsing/runtime-trigger assertions and
  `git diff --check`: passed.
- Upgrade warning fixes subsequently passed all **30 auth/filter tests**:
  use the current Starlette 422 constant and a valid-length wrong JWT key.
  Remaining passlib `crypt` deprecation is acknowledged, not suppressed.

Python **3.11.14**, after warning fixes and before lock-test integration, also
passed **220 application tests**
(including all 19 PostgreSQL cases), **32 SDK tests**, and **8 fixture tests**.
Only the acknowledged passlib deprecation warning remained. Runtime versions
in the 57-package deployment lock exactly match the tested Python 3.12
environment; its current advisory audit reported **no known vulnerabilities**.
That scan does not cover the container OS, bootstrap pip/setuptools, or
unpublished advisories.

The final default `make check`, after all code changes and lock-test integration,
passed on Python 3.12: **212 application tests passed, 19 PostgreSQL tests
deselected**, **32 SDK tests**, **8 fixture tests**, Ruff check/format
(**154 files**), configured mypy (**21 files**), the **57-pin** lock checker,
and compileall. The 19 PostgreSQL cases passed in the earlier full-suite gates
on both Python minors; the default gate does not rerun them. These are separate
runs, not one combined 231-test invocation.

Commands used (prepend the isolated environment's `bin` to PATH):

```sh
TEST_POSTGRES_URL=<scratch-only-url> python -m pytest app/tests -q
make lint typecheck sdk-test harness-test
mypy tests/egress/test_harness.py tests/egress/tls_fixture.py tests/egress/tls_receiver.py
DATABASE_URL=<scratch-only-url> make migration-check
python -m compileall -q app migrations scripts sdk/python/src examples tests/egress
python -m pip check
python -m build --outdir <scratch-output> sdk/python
python -m twine check <scratch-output>/*
```

Docker/container and hosted CI gates remain blocked/unexecuted as described
above. Do not equate these successful local gates with deployment approval.

## Dependency changes and remaining work

Security-led changes: FastAPI 0.115.6 → 0.142.2, explicit Starlette 1.7.0,
python-multipart 0.0.20 → 0.0.32, PyJWT 2.10.1 → 2.15.1,
pytest 8.3.4 → 9.1.1 and pytest-asyncio 0.25.0 → 1.4.0. Test tooling is
no longer shipped in runtime requirements. The receiver example and SDK test
extra follow the tested versions; SDK runtime dependencies are unchanged.

The 2026-10-02 advisory baseline included
[Starlette GHSA-82w8-qh3p-5jfq](https://github.com/advisories/GHSA-82w8-qh3p-5jfq),
[PyJWT GHSA-42vr-xj54-vc7v](https://github.com/advisories/GHSA-42vr-xj54-vc7v),
[multipart GHSA-v9pg-7xvm-68hf](https://github.com/advisories/GHSA-v9pg-7xvm-68hf)
and [pytest GHSA-6w46-j5rx-g56g](https://github.com/advisories/GHSA-6w46-j5rx-g56g).
The final 57-package runtime audit returned no known vulnerabilities without
ignored findings. This is a point-in-time scanner result, not a security guarantee.

Hash-checked Python 3.11 Linux x86_64 wheel downloads passed for all 57 runtime
packages. The helper also verified aarch64 downloads, a fresh hash-locked
3.11 install, offline direct-pin validation, and rejection of an intentionally
stale PyJWT pin. The new offline lock checker passed **11 tests on each of
Python 3.11 and 3.12**, including malformed hashes, duplicate names, floating
versions and changed/missing direct pins. Its scope is direct drift and syntax;
pip installation/`pip check` supply dependency completeness checks.

This is not an indiscriminate upgrade of every package. Routine DB, telemetry,
web-stack and image bumps remain held, as does the passlib/bcrypt migration.
Bootstrap pip/setuptools and container OS/image advisories need separate image
verification. Dependabot now covers SDK security updates, the receiver example
and proxy image, but its ability to regenerate this lock is unverified; stale
runtime pins fail CI rather than silently deploying a fresh resolution.
