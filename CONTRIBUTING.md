# Contributing

Thank you for improving the webhook platform. By participating, you agree to
follow `CODE_OF_CONDUCT.md`. Report vulnerabilities through `SECURITY.md`, not
public issues.

## Development setup

Requirements: Python 3.11+, Git, and PostgreSQL for concurrency or migration
checks. Docker is optional.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
make check
```

For PostgreSQL tests, use a local disposable database or a role permitted to
create and drop schemas:

```bash
export TEST_POSTGRES_URL='postgresql+asyncpg:///postgres'
pytest app/tests -q
```

The fixture creates a random schema and never drops the database or `public`
schema. Never point tests or benchmarks at production.

## Change workflow

1. Open or reference an issue for significant behavior changes.
2. Keep changes focused and preserve public API compatibility.
3. Add migrations for schema changes; never edit an applied migration.
4. Update documentation for API, configuration, security, or operations.
5. Run `make check`; run PostgreSQL tests for queue or transaction changes.
6. Use Conventional Commits, such as `fix(worker): reject stale leases`.

Do not commit `.env`, credentials, customer data, payloads, receiver responses,
database files, benchmark output, or generated virtual environments.
## Review requirements

Pull requests need a clear problem statement, risk assessment, validation
commands/results, and migration or rollback notes when applicable. At least one
maintainer approval and green required checks are expected before merge. Authors
must resolve security, tenant-isolation, data-loss, and backward-compatibility
concerns rather than accepting them as follow-up work.

Changes to retries must remain at idempotent boundaries with bounded backoff and
jitter. HTTP must remain outside database transactions. Worker and queue changes
need live PostgreSQL concurrency coverage. Egress changes need bypass analysis
and must not weaken the requirement for network enforcement.

## Documentation and release notes

Update `README.md` for user-facing behavior, `docs/threat-model.md` when trust
boundaries change, and `docs/release-policy.md` for compatibility decisions.
Call out deprecated behavior and operator action in the pull request.