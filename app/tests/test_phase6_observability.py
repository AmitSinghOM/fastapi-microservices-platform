import re

import pytest
from prometheus_client import generate_latest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Event
from app.observability import (
    EVENTS,
    QueueMetricsCollector,
    REGISTRY,
    set_runtime_role,
    validate_label_names,
)
from app.tests.test_delivery_contracts import seed_delivery
from app.tests.test_webhook_idempotency import create_project_key


def event_count(outcome: str, reason: str = "none") -> float:
    return EVENTS.labels("api", outcome, reason)._value.get()


def test_metric_label_guard_rejects_identifier_and_secret_labels():
    validate_label_names(("runtime_role", "status_class", "queue_kind"))
    for forbidden in ("tenant_id", "endpoint_url", "api_key", "payload"):
        with pytest.raises(ValueError, match="Forbidden metric label"):
            validate_label_names((forbidden,))


@pytest.mark.asyncio
async def test_event_outcomes_and_trace_context_are_observable(
    client,
    auth,
    db_session: AsyncSession,
):
    set_runtime_role("api")
    _, bearer = auth
    api_key = await create_project_key(client, bearer)
    accepted_before = event_count("accepted")
    idempotent_before = event_count("idempotent")
    conflict_before = event_count("rejected", "conflict")
    headers = {
        "X-API-Key": api_key,
        "Idempotency-Key": "phase6-trace-0001",
    }
    body = {"type": "phase6.test", "payload": {"safe": True}}
    first = await client.post("/v1/events", headers=headers, json=body)
    repeated = await client.post("/v1/events", headers=headers, json=body)
    conflict = await client.post(
        "/v1/events",
        headers=headers,
        json={"type": "phase6.changed", "payload": {}},
    )
    assert first.status_code == repeated.status_code == 202
    assert conflict.status_code == 409
    assert event_count("accepted") == accepted_before + 1
    assert event_count("idempotent") == idempotent_before + 1
    assert event_count("rejected", "conflict") == conflict_before + 1

    stored = await db_session.scalar(select(Event))
    assert stored is not None
    assert stored.traceparent is not None
    assert re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}",
        stored.traceparent,
    )

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "webhook_events_total" in metrics.text
    assert 'route="/v1/events"' in metrics.text
    for forbidden in (
        stored.public_id,
        "phase6.test",
        "phase6-trace-0001",
    ):
        assert forbidden not in metrics.text


@pytest.mark.asyncio
async def test_queue_collector_uses_database_state(
    db_session: AsyncSession,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
):
    del db_session
    set_runtime_role("worker")
    await seed_delivery(sqlite_session_factory)
    collector = QueueMetricsCollector(
        sqlite_session_factory, interval=60, role="worker"
    )
    await collector.collect_once()
    metrics = generate_latest(REGISTRY).decode()
    assert (
        'webhook_queue_depth{kind="runnable",runtime_role="worker"} 1.0'
        in metrics
    )
    assert (
        'webhook_queue_depth{kind="total",runtime_role="worker"} 1.0'
        in metrics
    )
    assert "webhook_queue_oldest_due_age_seconds" in metrics
