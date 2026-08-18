# Phase 0 PostgreSQL Baseline

## Result

The local one-million-delivery correctness gate passed on 2026-08-18. This is
baseline evidence, not a production capacity claim. The provisional p95 queue
age target of 30 seconds was **not** met.

## Reproduction

```bash
PHASE0_POSTGRES_URL=postgresql+asyncpg:///postgres \
  .venv/bin/python scripts/phase0_benchmark.py \
  --jobs 1000000 --api-events 250 --worker-sample 250 \
  --batch-size 5000 --confirmation ONE_MILLION_LOCAL_ONLY
```

The script accepts only local PostgreSQL hosts, creates a random schema, emits
JSON, and drops only that schema in `finally`. No benchmark schemas remained
after this run.

## Environment and workload

- Application base commit: `c3a81dc5db67f72f0b636b2d883b27a891836395`.
- The benchmark and webhook MVP were uncommitted working-tree changes.
- Requirements SHA-256: `b6f66e73fcb465dfd1cc228c93f176abd0cbebbd9297a914bdbf408094a74af9`.
- Host: macOS 26.5, Apple arm64, 8 logical CPUs, 16 GiB memory.
- Runtime: Python 3.12.13; PostgreSQL 17.10 (Homebrew).
- PostgreSQL maximum connections: 100; local Unix-socket connection.
- Topology: one in-process ASGI API and one worker runtime.
- API sample: 250 events, concurrency 10, fan-out 1.
- Real worker sample: 250 `DeliveryService` calls, concurrency 10, mocked
  healthy receiver with 5 ms latency.
- Bulk path: 999,750 generated jobs, 5,000-row claim/finalize batches using
  `FOR UPDATE SKIP LOCKED` and lease-token-guarded finalization.

## Measurements

| Signal | Result |
| --- | ---: |
| API accepted/rejected | 250 / 0 |
| API ingest rate | 110.16 events/s |
| API latency p50 / p95 / p99 | 81.32 / 162.45 / 181.19 ms |
| Real worker throughput | 161.98 deliveries/s |
| Real worker utilization | 59.25% |
| Bulk generation rate | 30,938.17 jobs/s |
| Bulk queue completion rate | 9,320.43 jobs/s |
| Overall generated-job completion rate | 7,085.91 jobs/s |
| Queue age p50 / p95 / p99 / max | 87.48 / 134.46 / 138.23 / 139.41 s |
| Peak benchmark PostgreSQL connections | 11 |
| WAL generated | 3,024,208,440 bytes (2.82 GiB) |
| Final schema relation size | 864,133,120 bytes (824.10 MiB) |

The bulk rate measures the benchmark's PostgreSQL batch path. It does not
represent end-to-end HTTP delivery capacity. Receiver latency, DNS, TLS,
network limits, and destination behavior require the Phase 10 load matrix.

## Correctness gate

- 1,000,000 expected, persisted, and succeeded delivery rows.
- 1,000,000 unique delivery public IDs; zero missing or duplicate jobs.
- 1,000,000 attempt rows and unique `(delivery_id, attempt_number)` pairs.
- Zero attempt-count mismatches and zero unexplained extra attempts.
- 250 accepted API events each retained exactly one fan-out delivery.
- 999,750 bulk jobs retained for the generated bulk event.
- Zero nonterminal rows and zero accepted stale-token finalizations.

The Phase 0 durability/correctness gate therefore passed. The p95 event
acceptance latency hypothesis passed for this small local sample, but the p95
oldest-job-age hypothesis failed under the one-million-job single-runtime run.
Phase 2 and later scaling work must improve queue behavior before any queue-age
SLO is claimed as achieved.
