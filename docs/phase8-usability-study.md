# Phase 8 independent usability study

> **Descoped 2026-09-08.** This protocol is no longer the Phase 8 completion
> gate: the project is maintained solo and no pool of independent testers is
> available. The gate is now a scripted clean-environment install and
> signed-delivery run (see the action plan). This document and its recorder
> script are retained unchanged so a human study can still be run if
> independent testers ever become available; until then, first-time human
> usability is unvalidated and no document claims otherwise.

This protocol measures the completion gate; it does not predeclare success.
Study result files are local evidence and must not be committed because they can
contain operational context. The recorder creates mode-`0600` files, uses atomic
replacement, rejects symlinks and group/world-readable files, and validates
stored timestamps against durations before reporting. Keep the parent directory
access-controlled and do not edit records by hand. Use pseudonymous participant
IDs and never record names, emails, tokens, API keys, signing secrets, endpoint
URLs, payloads, or receiver bodies.

## Participant eligibility

Recruit ten developers who did not implement or review the Phase 8 SDK, CLI,
examples, or setup guide. A participant is independent only if they have not
previously completed this platform flow and receive no private setup material.
Record ineligible trial runs, but do not count them toward the denominator.

Each participant gets a clean machine or clean virtual environment, the public
repository URL, and `docs/phase8-adoption.md`. The maintainer may observe but
must not answer setup questions, suggest commands, repair configuration, or
provide unpublished credentials after timing starts. If help occurs, record it;
the run cannot count as a success.

## Timer and success definition

Start timing when the participant first opens the adoption guide. Stop only
after all of the following are independently verified:

1. The SDK or repository package is installed in a clean Python environment.
2. The participant registers or logs in with `webhookctl`.
3. They create an organization, project, producer key, and HTTPS endpoint.
4. They send an event with a stable idempotency key.
5. Their receiver verifies the exact-body signature and durably accepts the
   event ID once.
6. They inspect the corresponding delivery or attempt through the CLI.

A passing run is strictly under 30 minutes, uses no maintainer help, and has a
verified signed delivery. Failed setup, an unverified delivery, exactly 30
minutes, or longer does not pass.

## Recording evidence

Initialize a local record outside source control:

```bash
python scripts/phase8_usability_study.py /secure/path/study.json init
```

Record one run with timezone-aware timestamps and a pseudonymous ID:

```bash
python scripts/phase8_usability_study.py /secure/path/study.json record \
  --participant-id participant-01 \
  --started-at 2026-08-24T10:00:00Z \
  --completed-at 2026-08-24T10:24:00Z \
  --independent \
  --no-maintainer-help \
  --signed-delivery-verified
```

For a failed run, use `--no-signed-delivery-verified` and a bounded, non-secret
`--failed-step`, such as `endpoint-create`. Generate the aggregate report:

```bash
python scripts/phase8_usability_study.py /secure/path/study.json report
```

The report exits zero only after at least ten eligible independent runs and at
least eight qualifying successes. A nonzero exit means the gate remains open;
it is not a test failure in the software.

## Follow-up

Aggregate failed steps and documentation feedback without attributing it to a
person. Fix repeatable usability problems, publish updated instructions, then
retest with new independent participants. Do not replace failed records or
reclassify participants to reach the threshold. Retain the original study file
in access-controlled project evidence and publish only non-sensitive aggregate
counts.

If a future human study is run, package-index publication and a portal are
not substitutes for its evidence. The release itself follows the ordered
[external-gate runbook](phase8-external-gates.md) — candidate freeze, owner
signing, remote controls, the scripted clean-machine gate, TestPyPI
cooling-off, and production release — with hosted checks green on the final
Phase 8 commit.
