# Phase 8 external gate execution

This runbook advances the remaining Phase 8 work without treating preparation
as human evidence. Execute sections in order. Phase 9 stays blocked until the
independent study and hosted release-candidate gates pass.

## Current snapshot — 2026-09-01

- The FastAPI Microservices Platform candidate is locally modified, uncommitted,
  and not frozen.
- The repository has one owner, no recorded rulesets, and no recorded
  `testpypi` or `pypi` environment.
- The current local Git configuration has no signing key or signed-commit
  default; do not enable enforcement until signing succeeds end to end.
- No signed SDK release tag, trusted publisher, package release, or participant
  record exists.
- Distribution availability checks are provisional, not legal or trademark
  clearance.

## 1. Establish signing capability

Configure a signing key without exposing private material. On a disposable
branch or separate test repository, create and push a signed test commit and a
signed annotated test tag. Verify that GitHub marks both as `Verified`, then
enable local commit signing for the final candidate. Do not create the candidate
commit or enable repository-wide signed-commit enforcement until this end-to-end
test succeeds. Delete or retire disposable references only through the owner's
normal reviewed process.

## 2. Create, validate, and freeze the signed candidate

1. Recheck `fastapi-microservices-platform-sdk` availability and complete the
   owner or qualified legal/name review immediately before publisher setup.
2. Review the final tree, create the Phase 8 candidate as a signed commit, push
   it, and verify GitHub marks that exact commit as `Verified`.
3. Obtain successful main-push CI for the exact full commit SHA.
4. Explicitly dispatch `container.yml` from `main` while it points at that
   commit, then verify the completed run's `head_sha` equals the candidate SHA.
   Container remains a manual Phase 8 evidence gate rather than an SDK workflow
   enforcement step.
5. Record and freeze the immutable full commit SHA. Do not change code or
   participant instructions during the cohort; material changes require a new
   signed candidate, hosted validation, frozen SHA, and cohort.
6. Confirm `sdk/python/pyproject.toml` version `0.1.0` corresponds to the planned
   signed annotated tag `sdk-v0.1.0`.

## 3. Configure remote controls

Only after owner signing works end to end, and with explicit authorization:

1. Protect `main` from force pushes and deletion, require signed commits and
   required CI, and disable administrator bypass where supported.
2. Protect `sdk-v*` tags from update or deletion and restrict tag creation to
   the repository owner.
3. Create `testpypi` and `pypi` deployment environments, allow deployments only
   from `sdk-v*` tags, and disable bypass where supported.
4. Add a 24-hour wait timer to `pypi`. This remote setting cannot be created by
   workflow YAML.
5. Configure TestPyPI and PyPI trusted publishers for distribution
   `fastapi-microservices-platform-sdk`, this repository, their respective
   workflow files, and exact environment names. Do not create API tokens.

These are high-impact remote security settings. Apply them only after final
workflows are committed and signing is proven, or the sole owner may be locked
out.

## 4. Run the independent cohort

Recruit ten developers satisfying `phase8-usability-study.md`. Give each only
the frozen public commit, `phase8-adoption.md`, and non-production access.
Schedule individually, prohibit maintainer help after timing starts, and record
all attempts—including failures—in one access-controlled mode-`0600` study
file. Never commit participant records or store names, contacts, credentials,
URLs, payloads, or receiver bodies.

Invitation template: “Evaluate a pre-release event-delivery workflow in a clean
environment. Plan for a 45-minute observed session; the measured task is
strictly under 30 minutes. No implementation experience is required. Results
are recorded under a pseudonymous ID without credentials or payload data.”

Run the recorder's `report` command. Continue only when ten eligible independent
runs exist and at least eight passed strictly under 30 minutes without help. A
failed gate requires fixes and a new independent cohort; never reclassify or
replace evidence.

## 5. Release to TestPyPI and cool off

From the frozen green commit, create and push a verified signed annotated tag.
Dispatch `python-sdk-test-release.yml` from that tag and pass the same tag as
its input (for example, `gh workflow run python-sdk-test-release.yml --ref
sdk-v0.1.0 -f tag=sdk-v0.1.0`). Verify the workflow used the
`testpypi` environment, built a clean checkout, passed import/CLI smoke checks,
and retained the exact wheel, source archive, and `SHA256SUMS` as one GitHub
artifact before publishing. On retry, it must restore those bytes rather than
rebuild the source archive. Its preflight must accept only an absent, matching
partial, or matching complete release; publication skips only preverified
existing files; and bounded post-publication polling must prove exactly the expected wheel and source distribution exist
with matching hashes and sizes and are not yanked. Record the workflow URL,
commit SHA, tag verification, artifact names, and hashes in release evidence.

Wait at least 24 hours after the newest TestPyPI artifact upload. Do not move or
replace the tag. During the cooling-off period, install from TestPyPI in a clean
environment, review metadata and license, inspect the hash manifest, and execute
the written release checklist.

## 6. Review and release to PyPI

Use a second authenticated device or fresh authenticated session to review the
signed tag, exact-commit CI, study aggregate, TestPyPI artifacts, hashes,
cooling-off timestamp, environment restrictions, and checklist. This reduces
session mistakes but is not independent approval.

Only then dispatch `python-sdk-release.yml` from the unchanged tag and pass the
same tag as its input. The workflow requires a successful exact-tag,
exact-commit TestPyPI run, retrieves that run's retained manifest, and downloads
only the expected non-yanked files from the official TestPyPI artifact host. It
verifies reported and downloaded sizes, manifest/API/download hashes, approved
HTTPS ports before and after redirects, and the minimum 24-hour age. A PyPI
preflight permits only absent or byte-identical partial/complete state;
idempotent publication repairs missing files; and bounded polling proves the
complete release before success. The protected `pypi` environment wait timer
still applies. Afterward, verify package metadata and clean installation without
modifying or deleting the released version.

FastAPI Microservices Platform is maintained and released by one repository
owner. No second steward or release approver is required or implied;
independent approval is not currently guaranteed. The independent 8-of-10
cohort remains product-usability evidence and cannot be simulated.
