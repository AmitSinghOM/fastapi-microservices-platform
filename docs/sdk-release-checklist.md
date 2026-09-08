# Python SDK release checklist

Complete every item for the exact immutable candidate. Store links, full SHAs,
and hashes in private release evidence; never store credentials.

## Signing capability

- [ ] Configure owner signing without exposing private key material.
- [ ] Push a signed test commit and signed annotated test tag on a disposable
      branch or separate repository; verify GitHub marks both as `Verified`.
- [ ] Enable local commit signing before creating the final candidate. Do not
      enable repository-wide enforcement until the end-to-end test succeeds.

## Candidate and identity

- [ ] Recheck `fastapi-microservices-platform-sdk` availability and complete the
      owner or qualified legal/name review.
- [ ] Confirm package version, `sdk-vMAJOR.MINOR.PATCH` tag, changelog, license,
      URLs, import `webhook_platform_sdk`, and CLI `webhookctl`.
- [ ] Create and push the final candidate as a signed commit and verify GitHub
      marks that exact commit as `Verified`.
- [ ] Record successful exact-SHA main-push CI and an explicitly dispatched
      Container run whose `head_sha` equals the candidate SHA.
- [ ] Freeze that SHA; any material change requires a new signed candidate,
      hosted validation, and a fresh freeze.
- [ ] Confirm the amended Phase 8 completion gate passed on the frozen
      candidate: `scripts/phase8_clean_machine_gate.py` green within its
      30-minute budget, with the JSON report retained as evidence (the
      8-of-10 independent-developer study was descoped on 2026-09-08; see
      the action plan's gate decision).

## Repository and publisher controls

- [ ] Confirm `main` and `sdk-v*` protections prevent force-push, deletion, and
      bypass as configured.
- [ ] Confirm `testpypi` and `pypi` environments allow only `sdk-v*` tags.
- [ ] Confirm `pypi` has a 24-hour wait timer and both publishers use OIDC only.

## TestPyPI

- [ ] Create `sdk-vMAJOR.MINOR.PATCH` as a signed annotated tag on the
      unchanged frozen candidate and verify GitHub marks it as `Verified`.
- [ ] Dispatch `python-sdk-test-release.yml` from that exact tag and pass the
      same tag as the workflow input.
- [ ] Confirm the workflow retained the exact wheel, source archive, and
      `SHA256SUMS` before publication; on retry, confirm it restored those bytes.
- [ ] Confirm preflight accepted only absent or matching partial/complete state
      and record the successful run URL plus its candidate artifact.
- [ ] Confirm post-publication verification found exactly the expected,
      non-yanked wheel and source distribution with matching sizes and hashes.
- [ ] Install the TestPyPI wheel in a clean Python 3.11+ environment and run
      import and `webhookctl --help` smoke checks.
- [ ] Wait at least 24 hours after the newest TestPyPI upload without moving the
      tag or changing the candidate.

## Production

- [ ] Review all evidence from a second authenticated device or fresh session.
- [ ] Confirm production selected the successful TestPyPI run for the exact
      tag and commit, downloaded its retained manifest, and matched manifest,
      TestPyPI API, and downloaded SHA-256 values.
- [ ] Confirm reported/downloaded sizes, non-yanked state, and approved
      TestPyPI HTTPS host and port before and after redirects.
- [ ] Dispatch `python-sdk-release.yml` from the unchanged tag and pass that
      same tag as the workflow input.
- [ ] Confirm PyPI preflight accepted only absent or matching partial/complete
      state and post-publication polling proved the exact complete release.
- [ ] Confirm the protected environment wait completed and PyPI OIDC succeeded.
- [ ] Install from PyPI cleanly, verify metadata/license/import/CLI, and record
      the final package URL and hashes.

Any mismatch, failed check, changed tag, or changed candidate stops the release.
Fix forward with a new SemVer version; never replace published files.
