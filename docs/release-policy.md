# Release and Compatibility Policy

## Versioning

The project uses Semantic Versioning. Maintainers create signed
`vMAJOR.MINOR.PATCH` application tags from a green `main` commit and publish
release notes with changes, migrations, security fixes, and known limitations.
The Python SDK uses independent signed `sdk-vMAJOR.MINOR.PATCH` tags whose
version must match `sdk/python/pyproject.toml`.

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

## Python SDK publication

FastAPI Microservices Platform is currently maintained and released by a single
repository owner. Releases use a signed final candidate commit and signed tag,
protected branches and tags, exact-commit CI verification, explicit exact-SHA
Container evidence, PyPI trusted publishing, protected deployment environments,
and a mandatory cooling-off period. Independent approval is not currently
guaranteed.

The owner first dispatches `Publish Python SDK to TestPyPI` for a signed
`sdk-vMAJOR.MINOR.PATCH` tag. The workflow verifies the tag, tagged version,
`main` containment, and successful exact-commit CI; builds from a clean
checkout; records and verifies SHA-256 hashes; retains the exact wheel, source
archive, and manifest as one GitHub artifact; smoke-tests the installed wheel;
and preflights TestPyPI before OIDC publication. A retry restores those original
bytes instead of rebuilding the non-reproducible source archive. Existing files
are skipped only after their names, hashes,
sizes, yanked state, and URLs match; bounded post-publication polling requires
the exact complete release. This makes retries repair a matching partial upload
and reject conflicting registry state.

Production dispatch is blocked until those TestPyPI artifacts have cooled for
at least 24 hours. The production workflow repeats source and CI checks, selects
a successful TestPyPI workflow run for the exact tag and commit, and retrieves
its retained manifest. It downloads only expected non-yanked files from the
official TestPyPI artifact host, verifies reported/downloaded sizes and
manifest/API/download SHA-256 values, smoke-tests the promoted wheel, then
applies the same preflight, idempotent upload, and bounded completion check to
PyPI through the protected `pypi` environment.

Before first use, configure separate `testpypi` and `pypi` environments and
trusted publishers. Restrict both environments to `sdk-v*` tags, disable
administrator bypass where supported, and configure a 24-hour wait timer on
`pypi`. GitHub workflow YAML cannot create those environment settings.
Configure and prove signing before creating the final candidate commit; enable
repository-wide signed-commit enforcement only after the sole owner has tested
the complete signing path. Long-lived package-index tokens are not supported,
and released filenames or versions are never replaced.

Production publication also remains blocked until the amended Phase 8
completion gate passes: a scripted clean-environment install and
signed-delivery run per the action plan (the former independent 8-of-10
study was descoped on 2026-09-08). That gate is product
evidence, not a second release-approver requirement. The owner must follow the
[SDK release checklist](sdk-release-checklist.md) and
[Phase 8 external-gate runbook](phase8-external-gates.md), including review from
a second authenticated device or session. This guidance summarizes the linked
[PyPI trusted publishing documentation](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
instead of reproducing it.

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
