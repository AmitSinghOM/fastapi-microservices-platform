# ADR 0003: Rename the repository to `fastapi-webhook-delivery-platform`

- Status: **Rejected** (2026-09-12, repository owner). ADR 0001's repository
  name stands. The owner judged the one-time coordination cost — GitHub
  rename, two trusted-publisher edits, seven link updates, portfolio and
  résumé changes, plus the risk of a mis-sequenced step breaking the next
  release — not worth the positioning gain, given that the README, package
  description, launch drafts, and portfolio already carry the webhook
  positioning in their first sentence. The analysis and runbook below are
  retained so the decision can be revisited with full context; if it is
  reopened, the runbook still applies unchanged.
- Date: 2026-09-12
- Decision owner: repository owner

## Context

ADR 0001 retained the repository name `fastapi-microservices-platform` on the
grounds that a webhook-only name would hide the system's scope. Since then the
2026-09-08 staff review and the Svix/Convoy comparison reached the opposite
conclusion for positioning: the product's differentiated claim is *webhook
delivery on PostgreSQL alone*, and a generic "microservices platform" name
makes the repository unfindable by the people it serves and invites the
"another framework" dismissal on first contact. The README, benchmarks page,
launch drafts, and portfolio already lead with "webhook delivery platform";
the repository name is the last surface that does not.

Two facts change the cost of a rename compared with 2026-08-24:

1. GitHub repository renames preserve issues, stars, releases, tags, commit
   SHAs, and Actions history, and redirect the old URL (web, `git clone`,
   API) indefinitely until a new repository reuses the old name.
2. The release pipeline does not hard-code the repository name: both release
   workflows call the GitHub API through `$GITHUB_REPOSITORY`, which follows
   the rename automatically.

The one external coupling that does **not** follow a rename is trusted
publishing. The PyPI and TestPyPI trusted publishers are registered against
the exact repository name `AmitSinghOM/fastapi-microservices-platform`; an
OIDC token issued from a renamed repository carries the new name and is
rejected at upload. This is why the rename was deliberately deferred until
`0.1.1` had reached PyPI on 2026-09-11.

## Decision

Rename the GitHub repository to **`fastapi-webhook-delivery-platform`** and
update the display name to **FastAPI Webhook Delivery Platform**.

Retain unchanged, exactly as in ADR 0001:

- Python distribution: `fastapi-microservices-platform-sdk` (already
  published; renaming a distribution creates a new PyPI project and forces
  every installer to migrate — the cost is not justified for a
  three-day-old package, and ADR 0001 already established that distribution
  and repository names need not match)
- Python import package: `webhook_platform_sdk`
- CLI executable: `webhookctl`
- SDK environment prefix: `WEBHOOK_PLATFORM_`
- SDK release tags: `sdk-vMAJOR.MINOR.PATCH`

## Migration runbook (owner-only, sequenced; do not reorder steps 1–3)

1. **Precondition:** no release run is in flight and no candidate is frozen.
   Confirm the latest `sdk-v*` tag has reached PyPI.
2. **Rename on GitHub:** Settings → General → Repository name →
   `fastapi-webhook-delivery-platform`. Verify the old URL redirects.
3. **Update both trusted publishers before any dispatch:**
   - PyPI → project `fastapi-microservices-platform-sdk` → Publishing →
     edit the GitHub publisher → repository name
     `fastapi-webhook-delivery-platform` (owner, workflow filename
     `python-sdk-release.yml`, environment `pypi` unchanged).
   - TestPyPI → same project → repository name updated, workflow
     `python-sdk-test-release.yml`, environment `testpypi`.
   Until this step completes, every release dispatch will fail at the
   upload step with an OIDC claim mismatch; that is the expected failure
   mode, not a pipeline defect.
4. **Update local remote:** `git remote set-url origin
   https://github.com/AmitSinghOM/fastapi-webhook-delivery-platform.git`
   (redirects would work, but the release policy audits exact URLs).
5. **Update hard-coded references in one docs commit** (7 lines, 6 files;
   enumerated by
   `grep -rn "AmitSinghOM/fastapi-microservices-platform\|fastapi-microservices-platform\b" --include=*.md --include=*.toml .`
   excluding the distribution name and ADR 0001):
   `README.md` CI badge and link, `SECURITY.md` and `CODE_OF_CONDUCT.md`
   advisory links, `docs/phase8-adoption.md` identity sentence,
   `sdk/python/pyproject.toml` `[project.urls]` (takes effect on PyPI at
   the next release, `0.1.2`), and the `title` and `repository` fields in
   any documentation front matter.
6. **Mark this ADR Accepted** with the rename date, and add a superseded
   note to ADR 0001's repository-name line pointing here.
7. **Portfolio and résumé** (`amit-singh.github.io`): update the two
   project links and the benchmarks/writeup links to the new URL. The old
   links continue to redirect, so this is a correctness fix, not a
   breakage fix.
8. **Verification:** dispatch `python-sdk-test-release` only when a real
   `0.1.2` candidate exists; a green TestPyPI run under the new name is the
   evidence that step 3 was completed correctly. Record it in `action.md`.

## Consequences

- The repository name finally matches the product positioning used
  everywhere else; search and first-impression cost drops.
- ADR 0001's reasoning about system scope is preserved in the README's
  architecture section rather than the repository name.
- One-time owner effort of roughly 20 minutes, plus a docs commit.
- Risk accepted (2026-09-12): a future repository created under the old
  name would break the redirect. Mitigation: do not reuse the old name;
  optionally create an archived placeholder repository under the old name
  whose README points to the new one, which also blocks squatting.
- Not doing this: the name remains the one surface contradicting the
  positioning, and every external link created from launch onward points
  at a name the project no longer wants.
