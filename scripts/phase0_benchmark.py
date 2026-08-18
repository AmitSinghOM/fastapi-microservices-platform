"""Disposable local PostgreSQL benchmark for the Phase 0 gate.

The benchmark refuses remote hosts, creates a random schema, and removes only
that schema. Standard output is one machine-readable JSON document; progress
is written to standard error.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.services.delivery_service as delivery_module  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ApiKey,
    Event,
    Organization,
    Project,
    WebhookEndpoint,
)
from app.services.delivery_service import DeliveryService  # noqa: E402
from app.webhook_security import (  # noqa: E402
    canonical_json,
    digest_api_key,
    generate_api_key,
)

MILLION_CONFIRMATION = "ONE_MILLION_LOCAL_ONLY"
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=10_000)
    parser.add_argument("--api-events", type=int, default=250)
    parser.add_argument("--api-concurrency", type=int, default=10)
    parser.add_argument("--worker-sample", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--receiver-latency-ms", type=float, default=5.0)
    parser.add_argument("--confirmation", default="")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    numeric = (
        args.jobs,
        args.api_events,
        args.api_concurrency,
        args.worker_sample,
        args.batch_size,
    )
    if any(value < 1 for value in numeric):
        raise SystemExit(
            "Job, API, worker, concurrency, and batch values must be >= 1"
        )
    if args.api_events > args.jobs:
        raise SystemExit("--api-events cannot exceed --jobs")
    if args.worker_sample > args.api_events:
        raise SystemExit("--worker-sample cannot exceed --api-events")
    if args.receiver_latency_ms < 0:
        raise SystemExit("--receiver-latency-ms cannot be negative")
    if args.jobs >= 1_000_000 and args.confirmation != MILLION_CONFIRMATION:
        raise SystemExit(
            "One million jobs require --confirmation " + MILLION_CONFIRMATION
        )


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[min(index, len(ordered) - 1)]


def checked_database_url() -> tuple[str, str]:
    value = os.getenv("PHASE0_POSTGRES_URL") or os.getenv(
        "TEST_POSTGRES_URL"
    )
    if not value:
        raise SystemExit("Set PHASE0_POSTGRES_URL or TEST_POSTGRES_URL")
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg":
        raise SystemExit("Benchmark URL must use postgresql+asyncpg")
    if url.host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise SystemExit(
            "Phase 0 benchmark refuses non-local PostgreSQL hosts"
        )
    dsn = url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    return value, dsn


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def requirements_sha256() -> str:
    path = PROJECT_ROOT / "requirements.txt"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def host_memory_bytes() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(
            os.sysconf("SC_PHYS_PAGES")
        )
    except (KeyError, OSError, TypeError, ValueError):
        return None


async def monitor_connections(
    dsn: str,
    stop: asyncio.Event,
    peak: list[int],
) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(
            "SET application_name = 'phase0_benchmark_monitor'"
        )
        while not stop.is_set():
            count = await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = 'phase0_benchmark'"
            )
            peak[0] = max(peak[0], int(count))
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.02)
            except TimeoutError:
                pass
    finally:
        await connection.close()


async def create_schema(database_url: str, schema: str) -> AsyncEngine:
    admin_engine = create_async_engine(
        database_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    return admin_engine


def benchmark_engine(
    database_url: str,
    schema: str,
    api_concurrency: int,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    pool_size = max(5, min(api_concurrency, 20))
    engine = create_async_engine(
        database_url,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "application_name": "phase0_benchmark",
            }
        },
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return engine, factory


async def seed_control_plane(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    run_id: str,
) -> tuple[int, int, str]:
    now = datetime.now(timezone.utc)
    plaintext, prefix = generate_api_key()
    async with factory() as session:
        async with session.begin():
            organization = Organization(
                public_id=str(uuid4()),
                name=f"Phase 0 {run_id[:8]}",
                created_at=now,
            )
            session.add(organization)
            await session.flush()
            project = Project(
                public_id=str(uuid4()),
                organization_id=organization.id,
                name="Benchmark",
                is_active=True,
                created_at=now,
            )
            session.add(project)
            await session.flush()
            endpoint = WebhookEndpoint(
                public_id=str(uuid4()),
                project_id=project.id,
                url="https://receiver.example/webhooks",
                description="Disposable benchmark receiver",
                is_active=True,
                secret_version=1,
                created_at=now,
                updated_at=now,
            )
            api_key = ApiKey(
                public_id=str(uuid4()),
                project_id=project.id,
                name="Benchmark key",
                key_prefix=prefix,
                key_digest=digest_api_key(
                    plaintext,
                    settings.api_key_pepper,
                ),
                is_active=True,
                created_at=now,
            )
            session.add_all((endpoint, api_key))
            await session.flush()
            return project.id, endpoint.id, plaintext


async def run_api_benchmark(
    factory: async_sessionmaker[AsyncSession],
    plaintext_key: str,
    run_id: str,
    event_count: int,
    concurrency: int,
) -> dict[str, Any]:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    semaphore = asyncio.Semaphore(concurrency)

    async def send_event(
        client: httpx.AsyncClient,
        index: int,
    ) -> tuple[float, int]:
        async with semaphore:
            started = time.perf_counter()
            response = await client.post(
                "/v1/events",
                headers={
                    "X-API-Key": plaintext_key,
                    "Idempotency-Key": f"phase0-api-{run_id}-{index}",
                },
                json={
                    "type": "phase0.benchmark",
                    "payload": {"index": index, "run_id": run_id},
                },
            )
            return time.perf_counter() - started, response.status_code

    app.dependency_overrides[get_db] = override_get_db
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            results = await asyncio.gather(
                *(send_event(client, index) for index in range(event_count))
            )
    finally:
        app.dependency_overrides.clear()
    elapsed = time.perf_counter() - started
    latencies = [duration * 1_000 for duration, _ in results]
    accepted = sum(status == 202 for _, status in results)
    statuses: dict[str, int] = {}
    for _, status in results:
        key = str(status)
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "requested": event_count,
        "accepted": accepted,
        "rejected": event_count - accepted,
        "statuses": statuses,
        "elapsed_seconds": elapsed,
        "events_per_second": accepted / elapsed,
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "mean": statistics.fmean(latencies),
        },
    }


async def run_worker_sample(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    sample_count: int,
    receiver_latency_ms: float,
) -> tuple[dict[str, Any], list[float]]:
    async def allow_benchmark_target(
        url: str,
        allow_http: bool = False,
    ) -> str:
        del allow_http
        return url

    async def receiver(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(receiver_latency_ms / 1_000)
        return httpx.Response(200, text="accepted")

    original_validator = delivery_module.validate_webhook_url
    delivery_module.validate_webhook_url = allow_benchmark_target
    durations: list[float] = []
    queue_ages: list[float] = []
    finalized = 0
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(receiver)
        ) as client:
            service = DeliveryService(factory, client, settings)
            while finalized < sample_count:
                limit = min(
                    settings.worker_concurrency,
                    sample_count - finalized,
                )
                claims = await service.claim_due(limit)
                if not claims:
                    break
                claimed_at = datetime.now(timezone.utc)
                queue_ages.extend(
                    max(
                        0.0,
                        (claimed_at - claim.event_created_at).total_seconds(),
                    )
                    for claim in claims
                )

                async def deliver_one(claim) -> bool:
                    delivery_started = time.perf_counter()
                    result = await service.deliver(claim)
                    durations.append(time.perf_counter() - delivery_started)
                    return result

                outcomes = await asyncio.gather(
                    *(deliver_one(claim) for claim in claims)
                )
                finalized += sum(outcomes)
    finally:
        delivery_module.validate_webhook_url = original_validator
    elapsed = time.perf_counter() - started
    capacity = elapsed * settings.worker_concurrency
    utilization = min(1.0, sum(durations) / capacity) if capacity else 0.0
    return (
        {
            "requested": sample_count,
            "succeeded": finalized,
            "elapsed_seconds": elapsed,
            "deliveries_per_second": finalized / elapsed,
            "configured_concurrency": settings.worker_concurrency,
            "utilization": utilization,
            "receiver_latency_ms": receiver_latency_ms,
        },
        queue_ages,
    )


async def create_bulk_event(
    factory: async_sessionmaker[AsyncSession],
    project_id: int,
    run_id: str,
) -> int:
    async with factory() as session:
        async with session.begin():
            created_at = datetime.now(timezone.utc)
            public_id = str(uuid4())
            payload = {"run_id": run_id, "bulk": True}
            event = Event(
                public_id=public_id,
                project_id=project_id,
                idempotency_key=f"phase0-bulk-{run_id}",
                event_type="phase0.bulk",
                payload=payload,
                payload_hash=hashlib.sha256(run_id.encode()).hexdigest(),
                canonical_envelope=canonical_json(
                    {
                        "id": public_id,
                        "type": "phase0.bulk",
                        "created_at": created_at.isoformat(),
                        "data": payload,
                    }
                ),
                created_at=created_at,
            )
            session.add(event)
            await session.flush()
            return event.id


async def connect_benchmark(dsn: str, schema: str) -> asyncpg.Connection:
    connection = await asyncpg.connect(dsn)
    await connection.execute("SET application_name = 'phase0_benchmark'")
    await connection.execute(f'SET search_path TO "{schema}"')
    return connection


async def generate_bulk_deliveries(
    connection: asyncpg.Connection,
    event_id: int,
    endpoint_id: int,
    run_id: str,
    count: int,
    batch_size: int,
) -> dict[str, Any]:
    inserted = 0
    started = time.perf_counter()
    created_at = datetime.now(timezone.utc)
    statement = """
        INSERT INTO deliveries (
            public_id, event_id, endpoint_id,
            endpoint_public_id_snapshot, endpoint_url_snapshot,
            endpoint_active_snapshot, signing_secret_version_snapshot,
            status, attempt_count, next_attempt_at, created_at, updated_at
        )
        SELECT
            md5($1 || series::text), $2, endpoint.id,
            endpoint.public_id, endpoint.url, endpoint.is_active,
            endpoint.secret_version, 'pending', 0, $4, $4, $4
        FROM generate_series($5::integer, $6::integer) AS series
        CROSS JOIN webhook_endpoints AS endpoint
        WHERE endpoint.id = $3
    """
    while inserted < count:
        chunk = min(batch_size, count - inserted)
        await connection.execute(
            statement,
            run_id,
            event_id,
            endpoint_id,
            created_at,
            inserted + 1,
            inserted + chunk,
        )
        inserted += chunk
        if inserted % 100_000 == 0 or inserted == count:
            print(
                f"generated {inserted:,}/{count:,} bulk jobs",
                file=sys.stderr,
            )
    elapsed = time.perf_counter() - started
    return {
        "generated": inserted,
        "elapsed_seconds": elapsed,
        "jobs_per_second": inserted / elapsed if elapsed else 0.0,
    }


CLAIM_SQL = """
    WITH due AS (
        SELECT id
        FROM deliveries
        WHERE (
            status IN ('pending', 'retry_scheduled')
            AND next_attempt_at <= clock_timestamp()
        ) OR (
            status = 'processing'
            AND lease_expires_at <= clock_timestamp()
        )
        ORDER BY next_attempt_at, id
        LIMIT $1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE deliveries AS delivery
    SET status = 'processing',
        attempt_count = delivery.attempt_count + 1,
        lease_token = md5(
            $2 || delivery.id::text || clock_timestamp()::text
        ),
        lease_expires_at = clock_timestamp() + interval '60 seconds',
        updated_at = clock_timestamp()
    FROM due
    WHERE delivery.id = due.id
    RETURNING delivery.id, delivery.lease_token, delivery.attempt_count,
        EXTRACT(EPOCH FROM (clock_timestamp() - delivery.created_at)) AS age
"""

FINALIZE_SQL = """
    WITH claimed AS (
        SELECT *
        FROM unnest($1::integer[], $2::text[], $3::integer[])
            AS row(id, lease_token, attempt_number)
    ), finalized AS (
        UPDATE deliveries AS delivery
        SET status = 'succeeded',
            next_attempt_at = clock_timestamp(),
            lease_token = NULL,
            lease_expires_at = NULL,
            last_http_status = 200,
            last_error = NULL,
            updated_at = clock_timestamp(),
            succeeded_at = clock_timestamp()
        FROM claimed
        WHERE delivery.id = claimed.id
          AND delivery.status = 'processing'
          AND delivery.lease_token = claimed.lease_token
        RETURNING delivery.id, claimed.attempt_number
    ), inserted AS (
        INSERT INTO delivery_attempts (
            delivery_id, attempt_number, started_at, finished_at,
            outcome, http_status, error, response_body
        )
        SELECT id, attempt_number, clock_timestamp(), clock_timestamp(),
            'succeeded', 200, NULL, 'benchmark'
        FROM finalized
        RETURNING delivery_id
    )
    SELECT count(*) FROM inserted
"""


async def drain_queue(
    connection: asyncpg.Connection,
    run_id: str,
    expected: int,
    batch_size: int,
) -> tuple[dict[str, Any], list[float]]:
    processed = 0
    queue_ages: list[float] = []
    stale_probe: tuple[list[int], list[str], list[int]] | None = None
    started = time.perf_counter()
    while processed < expected:
        rows = await connection.fetch(CLAIM_SQL, batch_size, run_id)
        if not rows:
            break
        ids = [int(row["id"]) for row in rows]
        tokens = [str(row["lease_token"]) for row in rows]
        attempts = [int(row["attempt_count"]) for row in rows]
        queue_ages.extend(float(row["age"]) for row in rows)
        finalized = int(
            await connection.fetchval(FINALIZE_SQL, ids, tokens, attempts)
        )
        processed += finalized
        if stale_probe is None:
            stale_probe = (ids[:1], tokens[:1], attempts[:1])
        if processed % 100_000 == 0 or processed == expected:
            print(
                f"completed {processed:,}/{expected:,} queued jobs",
                file=sys.stderr,
            )
    stale_finalize_accepted = 0
    if stale_probe:
        stale_finalize_accepted = int(
            await connection.fetchval(FINALIZE_SQL, *stale_probe)
        )
    elapsed = time.perf_counter() - started
    return (
        {
            "expected": expected,
            "completed": processed,
            "elapsed_seconds": elapsed,
            "deliveries_per_second": processed / elapsed if elapsed else 0.0,
            "stale_finalize_accepted": stale_finalize_accepted,
        },
        queue_ages,
    )


async def read_database_metadata(
    connection: asyncpg.Connection,
) -> dict[str, Any]:
    version = await connection.fetchval("SHOW server_version")
    max_connections = await connection.fetchval("SHOW max_connections")
    database_size = await connection.fetchval(
        "SELECT sum(pg_total_relation_size("
        "quote_ident(schemaname) || '.' || quote_ident(relname))) "
        "FROM pg_stat_user_tables "
        "WHERE schemaname = current_schema()"
    )
    table_stats = await connection.fetch(
        "SELECT relname, n_live_tup, n_dead_tup, autovacuum_count, "
        "autoanalyze_count FROM pg_stat_user_tables "
        "WHERE schemaname = current_schema() ORDER BY relname"
    )
    return {
        "version": str(version),
        "max_connections": int(max_connections),
        "schema_relation_bytes": int(database_size or 0),
        "table_stats": [dict(row) for row in table_stats],
    }


async def verify_invariants(
    connection: asyncpg.Connection,
    run_id: str,
    expected_jobs: int,
    expected_api_events: int,
    expected_bulk_jobs: int,
) -> dict[str, Any]:
    delivery = await connection.fetchrow(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE status = 'succeeded') AS succeeded, "
        "count(*) FILTER (WHERE status NOT IN ('succeeded', 'dead')) "
        "AS nonterminal, count(DISTINCT public_id) AS unique_public_ids "
        "FROM deliveries"
    )
    attempts = await connection.fetchrow(
        "SELECT count(*) AS total, "
        "count(DISTINCT (delivery_id, attempt_number)) AS unique_attempts "
        "FROM delivery_attempts"
    )
    api = await connection.fetchrow(
        "SELECT count(DISTINCT event.id) AS events, "
        "count(delivery.id) AS jobs "
        "FROM events AS event LEFT JOIN deliveries AS delivery "
        "ON delivery.event_id = event.id "
        "WHERE event.idempotency_key LIKE $1",
        f"phase0-api-{run_id}-%",
    )
    bulk_jobs = await connection.fetchval(
        "SELECT count(delivery.id) FROM events AS event "
        "JOIN deliveries AS delivery ON delivery.event_id = event.id "
        "WHERE event.idempotency_key = $1",
        f"phase0-bulk-{run_id}",
    )
    attempt_mismatches = await connection.fetchval(
        "SELECT count(*) FROM deliveries AS delivery LEFT JOIN ("
        "SELECT delivery_id, count(*) AS count FROM delivery_attempts "
        "GROUP BY delivery_id) AS attempts "
        "ON attempts.delivery_id = delivery.id "
        "WHERE delivery.attempt_count <> coalesce(attempts.count, 0)"
    )
    values = {
        "expected_jobs": expected_jobs,
        "delivery_rows": int(delivery["total"]),
        "succeeded_rows": int(delivery["succeeded"]),
        "nonterminal_rows": int(delivery["nonterminal"]),
        "unique_delivery_public_ids": int(delivery["unique_public_ids"]),
        "attempt_rows": int(attempts["total"]),
        "unique_attempts": int(attempts["unique_attempts"]),
        "attempt_count_mismatches": int(attempt_mismatches),
        "expected_api_events": expected_api_events,
        "api_event_rows": int(api["events"]),
        "api_delivery_rows": int(api["jobs"]),
        "expected_bulk_jobs": expected_bulk_jobs,
        "bulk_delivery_rows": int(bulk_jobs),
    }
    values["missing_jobs"] = max(0, expected_jobs - values["delivery_rows"])
    values["duplicate_jobs"] = (
        values["delivery_rows"] - values["unique_delivery_public_ids"]
    )
    values["unexplained_attempts"] = max(
        0,
        values["attempt_rows"] - expected_jobs,
    )
    values["passed"] = all(
        (
            values["delivery_rows"] == expected_jobs,
            values["succeeded_rows"] == expected_jobs,
            values["nonterminal_rows"] == 0,
            values["unique_delivery_public_ids"] == expected_jobs,
            values["attempt_rows"] == expected_jobs,
            values["unique_attempts"] == expected_jobs,
            values["attempt_count_mismatches"] == 0,
            values["api_event_rows"] == expected_api_events,
            values["api_delivery_rows"] == expected_api_events,
            values["bulk_delivery_rows"] == expected_bulk_jobs,
        )
    )
    return values


async def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    validate_args(args)
    database_url, dsn = checked_database_url()
    schema = f"phase0_benchmark_{uuid4().hex}"
    run_id = uuid4().hex
    settings = get_settings()
    run_started_at = datetime.now(timezone.utc).isoformat()
    admin_engine: AsyncEngine | None = None
    engine: AsyncEngine | None = None
    connection: asyncpg.Connection | None = None
    monitor_task: asyncio.Task | None = None
    monitor_stop = asyncio.Event()
    peak_connections = [0]
    result: dict[str, Any] | None = None

    try:
        admin_engine = await create_schema(database_url, schema)
        engine, factory = benchmark_engine(
            database_url,
            schema,
            args.api_concurrency,
        )
        async with engine.begin() as sqlalchemy_connection:
            await sqlalchemy_connection.run_sync(Base.metadata.create_all)
        project_id, endpoint_id, plaintext_key = await seed_control_plane(
            factory,
            settings,
            run_id,
        )
        connection = await connect_benchmark(dsn, schema)
        wal_start = await connection.fetchval("SELECT pg_current_wal_lsn()")
        monitor_task = asyncio.create_task(
            monitor_connections(dsn, monitor_stop, peak_connections)
        )

        print("running real ASGI ingestion sample", file=sys.stderr)
        api_metrics = await run_api_benchmark(
            factory,
            plaintext_key,
            run_id,
            args.api_events,
            args.api_concurrency,
        )
        print("running real DeliveryService worker sample", file=sys.stderr)
        worker_started = time.perf_counter()
        worker_metrics, sample_queue_ages = await run_worker_sample(
            factory,
            settings,
            args.worker_sample,
            args.receiver_latency_ms,
        )

        bulk_count = args.jobs - args.api_events
        bulk_metrics = {
            "generated": 0,
            "elapsed_seconds": 0.0,
            "jobs_per_second": 0.0,
        }
        if bulk_count:
            bulk_event_id = await create_bulk_event(
                factory,
                project_id,
                run_id,
            )
            bulk_metrics = await generate_bulk_deliveries(
                connection,
                bulk_event_id,
                endpoint_id,
                run_id,
                bulk_count,
                args.batch_size,
            )

        queued_count = args.jobs - worker_metrics["succeeded"]
        queue_metrics, bulk_queue_ages = await drain_queue(
            connection,
            run_id,
            queued_count,
            args.batch_size,
        )
        worker_elapsed = time.perf_counter() - worker_started
        queue_ages = sample_queue_ages + bulk_queue_ages
        invariants = await verify_invariants(
            connection,
            run_id,
            args.jobs,
            args.api_events,
            bulk_count,
        )
        wal_bytes = int(
            await connection.fetchval(
                "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), $1::pg_lsn)",
                wal_start,
            )
        )
        database = await read_database_metadata(connection)
        one_million_gate = (
            args.jobs >= 1_000_000
            and invariants["passed"]
            and queue_metrics["stale_finalize_accepted"] == 0
        )
        result = {
            "run": {
                "schema": schema,
                "started_at": run_started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "jobs": args.jobs,
                "api_events": args.api_events,
                "worker_sample": args.worker_sample,
                "batch_size": args.batch_size,
                "git_commit": git_commit(),
                "requirements_sha256": requirements_sha256(),
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "memory_bytes": host_memory_bytes(),
                "api_replicas": 1,
                "worker_replicas": 1,
                "database": database,
            },
            "api": api_metrics,
            "worker_sample": worker_metrics,
            "bulk_generation": bulk_metrics,
            "queue": {
                **queue_metrics,
                "age_seconds": {
                    "p50": percentile(queue_ages, 0.50),
                    "p95": percentile(queue_ages, 0.95),
                    "p99": percentile(queue_ages, 0.99),
                    "max": max(queue_ages, default=0.0),
                },
                "overall_completion_rate": (
                    args.jobs / worker_elapsed if worker_elapsed else 0.0
                ),
            },
            "postgresql": {
                "peak_benchmark_connections": peak_connections[0],
                "wal_bytes": wal_bytes,
            },
            "invariants": invariants,
            "completion": {
                "correctness_invariants_passed": invariants["passed"],
                "one_million_gate_passed": one_million_gate,
            },
        }
    finally:
        app.dependency_overrides.clear()
        monitor_stop.set()
        if monitor_task is not None:
            await monitor_task
        if connection is not None:
            await connection.close()
        if engine is not None:
            await engine.dispose()
        if admin_engine is not None:
            try:
                async with admin_engine.connect() as admin_connection:
                    await admin_connection.execute(
                        text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                    )
            finally:
                await admin_engine.dispose()

    if result is None:
        raise RuntimeError("Benchmark did not produce a result")
    result["run"]["schema_cleaned_up"] = True
    return result


async def main() -> None:
    result = await benchmark(parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
