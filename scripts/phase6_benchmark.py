"""Local-only Phase 6 10x burst and healthy-tenant drain evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.services.delivery_service as delivery_module  # noqa: E402
from app.admission import (  # noqa: E402
    endpoint_quota_values,
    tenant_quota_values,
)
from app.config import Settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    Delivery,
    EndpointQuotaState,
    Event,
    Organization,
    Project,
    TenantQuotaState,
    WebhookEndpoint,
)
from app.services.delivery_service import DeliveryService  # noqa: E402
from app.worker import run_delivery_loop  # noqa: E402
from app.webhook_security import canonical_json  # noqa: E402

CONFIRMATION = "PHASE6_LOCAL_ONLY"
logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--arrival-seconds", type=float, default=60.0)
    parser.add_argument("--drain-slo-seconds", type=float, default=300.0)
    parser.add_argument("--receiver-latency-ms", type=float, default=5.0)
    parser.add_argument("--confirmation", default="")
    return parser.parse_args()


def checked_url() -> str:
    value = os.getenv("PHASE6_POSTGRES_URL") or os.getenv("TEST_POSTGRES_URL")
    if not value:
        raise SystemExit("Set PHASE6_POSTGRES_URL or TEST_POSTGRES_URL")
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg":
        raise SystemExit("Phase 6 requires postgresql+asyncpg")
    if url.host not in {None, "localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Phase 6 benchmark refuses non-local PostgreSQL")
    return value


def validate_args(args: argparse.Namespace) -> None:
    if args.confirmation != CONFIRMATION:
        raise SystemExit(f"Pass --confirmation {CONFIRMATION}")
    if args.events != 10_000:
        raise SystemExit(
            "The declared Phase 6 gate requires exactly 10,000 events"
        )
    if args.arrival_seconds <= 0 or args.drain_slo_seconds <= 0:
        raise SystemExit("Arrival and drain windows must be positive")
    if args.receiver_latency_ms < 0:
        raise SystemExit("Receiver latency cannot be negative")


async def allow_local_benchmark(url: str, allow_http: bool = False) -> str:
    del allow_http
    return url


async def seed_control_plane(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[Project, WebhookEndpoint, Project, WebhookEndpoint]:
    now = datetime.now(timezone.utc)
    async with factory() as session:
        burst_org = Organization(
            public_id=str(uuid4()), name="Burst", created_at=now
        )
        healthy_org = Organization(
            public_id=str(uuid4()), name="Healthy", created_at=now
        )
        session.add_all((burst_org, healthy_org))
        await session.flush()
        burst_project = Project(
            public_id=str(uuid4()),
            organization_id=burst_org.id,
            name="Burst",
            is_active=True,
            created_at=now,
        )
        healthy_project = Project(
            public_id=str(uuid4()),
            organization_id=healthy_org.id,
            name="Healthy",
            is_active=True,
            created_at=now,
        )
        session.add_all((burst_project, healthy_project))
        await session.flush()
        burst_endpoint = WebhookEndpoint(
            public_id=str(uuid4()),
            project_id=burst_project.id,
            url="https://receiver.example/hook",
            is_active=True,
            secret_version=1,
            created_at=now,
            updated_at=now,
        )
        healthy_endpoint = WebhookEndpoint(
            public_id=str(uuid4()),
            project_id=healthy_project.id,
            url="https://receiver.example/hook",
            is_active=True,
            secret_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add_all((burst_endpoint, healthy_endpoint))
        await session.flush()

        session.add_all(
            (
                TenantQuotaState(
                    **tenant_quota_values(burst_org.id, settings, now)
                ),
                TenantQuotaState(
                    **tenant_quota_values(healthy_org.id, settings, now)
                ),
                EndpointQuotaState(
                    **endpoint_quota_values(burst_endpoint.id, settings, now)
                ),
                EndpointQuotaState(
                    **endpoint_quota_values(
                        healthy_endpoint.id, settings, now
                    )
                ),
            )
        )
        await session.commit()
        return burst_project, burst_endpoint, healthy_project, healthy_endpoint


async def add_healthy_delivery(
    factory: async_sessionmaker[AsyncSession],
    project: Project,
    endpoint: WebhookEndpoint,
    organization_id: int,
    public_id: str,
    due_at: datetime,
) -> None:
    envelope = canonical_json(
        {"id": public_id, "type": "phase6.healthy", "data": {}}
    )
    async with factory() as session:
        event = Event(
            public_id=str(uuid4()),
            project_id=project.id,
            idempotency_key=public_id,
            event_type="phase6.healthy",
            payload={},
            payload_hash="0" * 64,
            canonical_envelope=envelope,
            created_at=due_at,
        )
        session.add(event)
        await session.flush()
        session.add(
            Delivery(
                public_id=public_id,
                organization_id=organization_id,
                event_id=event.id,
                endpoint_id=endpoint.id,
                endpoint_public_id_snapshot=endpoint.public_id,
                endpoint_url_snapshot=endpoint.url,
                endpoint_active_snapshot=True,
                signing_secret_version_snapshot=1,
                status="pending",
                attempt_count=0,
                next_attempt_at=due_at,
                created_at=due_at,
                updated_at=due_at,
            )
        )
        await session.commit()


async def generate_burst(
    factory: async_sessionmaker[AsyncSession],
    project: Project,
    endpoint: WebhookEndpoint,
    organization_id: int,
    started_at: datetime,
    events: int,
    arrival_seconds: float,
) -> None:
    statement = text("""
        WITH generated AS (
            INSERT INTO events (
                public_id, project_id, idempotency_key, event_type, payload,
                payload_hash, canonical_envelope, created_at
            )
            SELECT md5('phase6-event-' || series::text), :project_id,
                'phase6-' || series::text, 'phase6.burst', '{}'::json,
                repeat('0', 64), convert_to('{}', 'UTF8'),
                CAST(:started_at AS timestamptz)
                    + (:arrival_seconds * series / :events)
                    * interval '1 second'
            FROM generate_series(1, :events) AS series
            RETURNING id, public_id, created_at
        )
        INSERT INTO deliveries (
            public_id, organization_id, event_id, endpoint_id,
            endpoint_public_id_snapshot, endpoint_url_snapshot,
            endpoint_active_snapshot, signing_secret_version_snapshot,
            status, attempt_count, next_attempt_at, created_at, updated_at
        )
        SELECT md5('phase6-delivery-' || generated.public_id),
            :organization_id, generated.id, :endpoint_id,
            :endpoint_public_id, :endpoint_url,
            true, 1, 'pending', 0, generated.created_at,
            generated.created_at, generated.created_at
        FROM generated
    """)
    async with factory() as session:
        await session.execute(
            statement,
            {
                "project_id": project.id,
                "organization_id": organization_id,
                "endpoint_id": endpoint.id,
                "endpoint_public_id": endpoint.public_id,
                "endpoint_url": endpoint.url,
                "started_at": started_at,
                "arrival_seconds": arrival_seconds,
                "events": events,
            },
        )
        await session.commit()


async def wait_for_delivery(
    factory: async_sessionmaker[AsyncSession], public_id: str
) -> datetime:
    while True:
        async with factory() as session:
            value = await session.scalar(
                select(Delivery.succeeded_at).where(
                    Delivery.public_id == public_id
                )
            )
        if value is not None:
            return (
                value
                if value.tzinfo
                else value.replace(tzinfo=timezone.utc)
            )
        await asyncio.sleep(0.02)


async def wait_for_all(
    factory: async_sessionmaker[AsyncSession],
    expected: int,
    deadline_seconds: float,
    engine,
) -> tuple[datetime, float, int]:
    deadline = time.monotonic() + deadline_seconds
    peak_oldest_age = 0.0
    peak_checked_out = 0
    while time.monotonic() < deadline:
        now = datetime.now(timezone.utc)
        async with factory() as session:
            succeeded = int(
                await session.scalar(
                    select(func.count(Delivery.id)).where(
                        Delivery.status == "succeeded"
                    )
                )
                or 0
            )
            oldest = await session.scalar(
                select(func.min(Delivery.next_attempt_at)).where(
                    Delivery.status.in_(("pending", "retry_scheduled")),
                    Delivery.next_attempt_at <= now,
                )
            )
        if oldest is not None:
            oldest = (
                oldest
                if oldest.tzinfo
                else oldest.replace(tzinfo=timezone.utc)
            )
            peak_oldest_age = max(
                peak_oldest_age, (now - oldest).total_seconds()
            )
        peak_checked_out = max(
            peak_checked_out, engine.sync_engine.pool.checkedout()
        )
        if succeeded == expected:
            return now, peak_oldest_age, peak_checked_out
        await asyncio.sleep(0.05)
    raise TimeoutError("Burst did not drain inside the declared deadline")


async def verify(
    factory: async_sessionmaker[AsyncSession], expected: int
) -> dict[str, int]:
    async with factory() as session:
        deliveries = int(
            await session.scalar(select(func.count(Delivery.id))) or 0
        )
        succeeded = int(
            await session.scalar(
                select(func.count(Delivery.id)).where(
                    Delivery.status == "succeeded"
                )
            )
            or 0
        )
        attempts = int(
            await session.scalar(
                text("SELECT count(*) FROM delivery_attempts")
            )
            or 0
        )
        mismatches = int(
            await session.scalar(
                text("""
                    SELECT count(*) FROM deliveries d LEFT JOIN (
                        SELECT delivery_id, count(*) AS count
                        FROM delivery_attempts GROUP BY delivery_id
                    ) a ON a.delivery_id = d.id
                    WHERE d.attempt_count <> coalesce(a.count, 0)
                """)
            )
            or 0
        )
    return {
        "expected": expected,
        "deliveries": deliveries,
        "succeeded": succeeded,
        "attempts": attempts,
        "attempt_mismatches": mismatches,
    }


async def benchmark(args: argparse.Namespace) -> dict[str, object]:
    validate_args(args)
    database_url = checked_url()
    schema = f"phase6_benchmark_{uuid4().hex}"
    admin = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
    engine = None
    worker_task = None
    client: httpx.AsyncClient | None = None
    stop = asyncio.Event()
    original_validator = delivery_module.validate_webhook_url
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            database_url,
            connect_args={
                "server_settings": {
                    "search_path": schema,
                    "application_name": "phase6_benchmark",
                }
            },
            pool_size=5,
            max_overflow=0,
            pool_timeout=2,
        )
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        settings = Settings(
            environment="test",
            database_url=database_url,
            auto_create_schema=False,
            secret_key="s" * 32,
            api_key_pepper="p" * 32,
            webhook_signing_key="w" * 32,
            allow_http_webhooks=False,
            worker_poll_seconds=0.05,
            worker_batch_size=10,
            worker_concurrency=10,
            worker_global_concurrency=10,
            worker_egress_connection_budget=10,
            worker_lease_seconds=60,
            worker_heartbeat_seconds=10,
            worker_attempt_timeout_seconds=30,
            worker_finalization_margin_seconds=5,
            worker_shutdown_grace_seconds=35,
            worker_db_pool_size=5,
            worker_db_max_overflow=0,
            api_db_pool_size=1,
            api_db_max_overflow=0,
            database_connection_budget=20,
            tenant_in_flight_deliveries=10,
            endpoint_concurrency=10,
            endpoint_rate_per_second=10_000,
            endpoint_rate_burst=10_000,
            nat_connection_budget=10,
            observability_enabled=False,
        )
        burst_project, burst_endpoint, healthy_project, healthy_endpoint = (
            await seed_control_plane(factory, settings)
        )
        baseline_id = str(uuid4())
        baseline_due = datetime.now(timezone.utc)
        await add_healthy_delivery(
            factory,
            healthy_project,
            healthy_endpoint,
            healthy_project.organization_id,
            baseline_id,
            baseline_due,
        )

        async def receiver(request: httpx.Request) -> httpx.Response:
            del request
            await asyncio.sleep(args.receiver_latency_ms / 1_000)
            return httpx.Response(200, text="accepted")

        delivery_module.validate_webhook_url = allow_local_benchmark
        client = httpx.AsyncClient(transport=httpx.MockTransport(receiver))
        service = DeliveryService(factory, client, settings, engine=engine)
        worker_task = asyncio.create_task(
            run_delivery_loop(service, settings, stop)
        )
        baseline_finished = await wait_for_delivery(factory, baseline_id)
        baseline_latency = (baseline_finished - baseline_due).total_seconds()

        burst_started = datetime.now(timezone.utc) + timedelta(seconds=1)
        comparison_due = burst_started + timedelta(
            seconds=args.arrival_seconds / 2
        )
        comparison_id = str(uuid4())
        await add_healthy_delivery(
            factory,
            healthy_project,
            healthy_endpoint,
            healthy_project.organization_id,
            comparison_id,
            comparison_due,
        )
        await generate_burst(
            factory,
            burst_project,
            burst_endpoint,
            burst_project.organization_id,
            burst_started,
            args.events,
            args.arrival_seconds,
        )
        expected = args.events + 2
        comparison_task = asyncio.create_task(
            wait_for_delivery(factory, comparison_id)
        )
        completed_at, peak_oldest_age, peak_checked_out = await wait_for_all(
            factory,
            expected,
            args.arrival_seconds + args.drain_slo_seconds + 30,
            engine,
        )
        comparison_finished = await comparison_task
        comparison_latency = (
            comparison_finished - comparison_due
        ).total_seconds()
        arrival_end = burst_started + timedelta(
            seconds=args.arrival_seconds
        )
        drain_seconds = max(0.0, (completed_at - arrival_end).total_seconds())
        invariants = await verify(factory, expected)
        healthy_limit = max(0.25, baseline_latency * 2)
        passed = all(
            (
                invariants["deliveries"] == expected,
                invariants["succeeded"] == expected,
                invariants["attempts"] == expected,
                invariants["attempt_mismatches"] == 0,
                drain_seconds <= args.drain_slo_seconds,
                comparison_latency <= healthy_limit,
                peak_checked_out <= 5,
            )
        )
        stop.set()
        await asyncio.wait_for(worker_task, timeout=10)
        worker_task = None
        await client.aclose()
        return {
            "run": {
                "events": args.events,
                "arrival_seconds": args.arrival_seconds,
                "receiver_latency_ms": args.receiver_latency_ms,
                "schema": schema,
            },
            "queue": {
                "drain_seconds_after_arrival": drain_seconds,
                "drain_slo_seconds": args.drain_slo_seconds,
                "peak_oldest_due_age_seconds": peak_oldest_age,
            },
            "healthy_tenant": {
                "isolated_latency_seconds": baseline_latency,
                "burst_latency_seconds": comparison_latency,
                "limit_seconds": healthy_limit,
            },
            "database": {
                "peak_checked_out_connections": peak_checked_out,
                "pool_capacity": 5,
                "pool_exhausted": peak_checked_out > 5,
            },
            "invariants": invariants,
            "completion_gate_passed": passed,
        }
    finally:
        delivery_module.validate_webhook_url = original_validator
        stop.set()
        if worker_task is not None:
            await asyncio.gather(worker_task, return_exceptions=True)
        if client is not None:
            await client.aclose()
        if engine is not None:
            await engine.dispose()
        try:
            async with admin.connect() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await admin.dispose()


async def main() -> None:
    result = await benchmark(parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["completion_gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
