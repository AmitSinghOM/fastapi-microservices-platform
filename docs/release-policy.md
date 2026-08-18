# Release and Compatibility Policy

## Versioning

The project uses Semantic Versioning. Until release automation is added,
maintainers create signed `vMAJOR.MINOR.PATCH` tags from a green `main` commit
and publish release notes with changes, migrations, security fixes, and known
limitations.

- Patch: compatible fixes and security updates.
- Minor: backward-compatible features and deprecations.
- Major: intentional incompatible API, configuration, or data changes.

The API reports the application version from `APP_VERSION`. Container tags
should include the exact version and immutable commit SHA; `latest` is not an
upgrade policy.

## Compatibility

Document request/response, configuration, environment, signature, and database
contract changes. Keep deprecated behavior for at least one minor release when
security and correctness permit. Never silently change webhook signature bytes,
idempotency meaning, or delivery guarantees.

The JWT `/users`, `/auth`, and tutorial `/items` APIs predate the webhook
product. `/items` remains enabled by default in 3.x for compatibility and can be
disabled with `EXAMPLE_ITEMS_ENABLED=false`. The default will become disabled
in 4.0, with removal no earlier than 5.0.

## Upgrade process

1. Read release notes and back up PostgreSQL.
2. Test the release and migrations against a restored non-production copy.
3. Run `alembic upgrade head` before starting the new API and workers.
4. Deploy API and workers using the documented compatibility window.
5. Verify readiness, queue age, failures, and stale leases.
6. Roll application code forward to fix migration failures; do not improvise
   destructive downgrades against production data.

## Support and vulnerabilities

Only the latest 3.x release receives security fixes. A security issue may
accelerate deprecation or require an incompatible release. Advisories identify
affected versions, mitigations, fixed versions, and upgrade instructions.
Release support expands only after maintainers can sustain it.
