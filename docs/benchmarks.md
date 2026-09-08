# Benchmarks and evidence

Every number on this page comes from a reproducible run documented in this
repository, with the reproduction command included. Nothing here is a
production capacity claim, and the one target we missed is reported as
missed. If you benchmark this platform and get different numbers, please
open an issue with your environment — that is exactly the conversation this
page exists to start.

## The one-million-delivery correctness gate (passed)

On a laptop-class machine (Apple arm64, 8 logical CPUs, 16 GiB, PostgreSQL
17.10), a single API runtime and a single worker runtime processed
**1,000,000 deliveries with zero loss and zero unexplained duplicates**:

- 1,000,000 expected, persisted, and succeeded delivery rows
- 1,000,000 unique delivery IDs; zero missing or duplicate jobs
- 1,000,000 attempt rows with unique `(delivery_id, attempt_number)` pairs
- Zero nonterminal rows; zero accepted stale-token finalizations
- Peak PostgreSQL connections: 11; WAL generated: 2.82 GiB

Full methodology, environment, and reproduction command:
[Phase 0 baseline](phase0-baseline.md). Dedicated crash-injection tests
separately prove stale-lease recovery, stale-finalization rejection, and the
documented at-least-once duplicate window
([delivery semantics](delivery-semantics.md)).

## Measured throughput (local baseline, not capacity)

| Signal | Result |
| --- | ---: |
| API ingest rate (250-event sample, concurrency 10) | 110 events/s |
| API latency p50 / p95 | 81 / 162 ms |
| Real worker delivery throughput (5 ms mock receiver) | 162 deliveries/s |
| Bulk queue completion rate (PostgreSQL batch path) | 9,320 jobs/s |
| Overall generated-job completion rate | 7,086 jobs/s |

The bulk rate measures the PostgreSQL claim/finalize path
(`FOR UPDATE SKIP LOCKED` with lease-token-guarded finalization), not
end-to-end HTTP delivery. Network, TLS, DNS, and receiver behavior are
deliberately excluded until the Phase 10 load matrix.

## The target we missed (reported honestly)

The provisional queue-age SLO of 30 seconds p95 was **not met**: under the
one-million-job single-runtime run, queue age was p50 87.5 s, p95 134.5 s,
p99 138.2 s. The action plan refuses to present this as achieved; improving
queue drain under backlog is tracked work, and no queue-age SLO will be
claimed until a measured run meets it.

## Install-to-signed-delivery time (scripted, repeatable)

The Phase 8 clean-machine gate
(`scripts/phase8_clean_machine_gate.py`) starts from a fresh clone and a
fresh virtual environment and completes the entire adoption flow —
install, authentication, organization/project/key/endpoint creation, a
signed event, a real worker delivery, exactly-once durable receiver
acceptance, and CLI inspection — unattended. Two consecutive runs finished
in **33.8 s and 31.9 s** against a 30-minute budget. The JSON report
contains step timings only.

## Wire-format verification

`standard`-scheme deliveries are cross-verified in CI by the official
`standardwebhooks` Python library — not only by this repository's own SDK —
over both crafted vectors and actual worker-emitted requests
([wire protocol](wire-protocol.md), golden vectors included). The legacy
scheme is byte-for-byte regression-locked.

## What this page does not claim

No multi-replica scaling results, no hosted-service uptime history, no
receiver-latency load matrix, no exactly-once delivery (the platform is
deliberately at-least-once), and no production capacity numbers. Those
belong to Phase 9/10 evidence when it exists, not to extrapolation.
