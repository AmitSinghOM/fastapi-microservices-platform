"""Low-cardinality metrics, safe tracing, and authoritative queue gauges."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import and_, case, event, func, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

logger = logging.getLogger("app.observability")
REGISTRY = CollectorRegistry(auto_describe=True)
_FORBIDDEN_LABEL_PARTS = {
    "tenant",
    "organization",
    "project",
    "event",
    "delivery",
    "endpoint",
    "url",
    "payload",
    "secret",
    "token",
    "key",
    "sql",
    "response_body",
}
_runtime_role = "api"
_provider: TracerProvider | None = None
_instrumented_engines: set[int] = set()
_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_label_names(names: tuple[str, ...]) -> None:
    """Reject labels that can invite identifiers or sensitive values."""
    for name in names:
        if not _LABEL_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid metric label name: {name}")
        parts = set(name.split("_"))
        if parts & _FORBIDDEN_LABEL_PARTS:
            raise ValueError(f"Forbidden metric label name: {name}")


def _counter(name: str, help_text: str, *labels: str) -> Counter:
    validate_label_names(labels)
    return Counter(name, help_text, labels, registry=REGISTRY)


def _gauge(name: str, help_text: str, *labels: str) -> Gauge:
    validate_label_names(labels)
    return Gauge(name, help_text, labels, registry=REGISTRY)


def _histogram(
    name: str,
    help_text: str,
    *labels: str,
    buckets: tuple[float, ...] | None = None,
) -> Histogram:
    validate_label_names(labels)
    if buckets is None:
        return Histogram(name, help_text, labels, registry=REGISTRY)
    return Histogram(
        name, help_text, labels, buckets=buckets, registry=REGISTRY
    )


HTTP_REQUESTS = _counter(
    "webhook_http_requests_total",
    "HTTP requests",
    "runtime_role",
    "method",
    "route",
    "status_class",
)
HTTP_DURATION = _histogram(
    "webhook_http_request_duration_seconds",
    "HTTP request duration",
    "runtime_role",
    "method",
    "route",
)
EVENTS = _counter(
    "webhook_events_total",
    "Event admission outcomes",
    "runtime_role",
    "outcome",
    "reason",
)
QUEUE_DEPTH = _gauge(
    "webhook_queue_depth",
    "Authoritative database queue depth",
    "runtime_role",
    "kind",
)
QUEUE_OLDEST = _gauge(
    "webhook_queue_oldest_due_age_seconds",
    "Age of oldest runnable job",
    "runtime_role",
)
DELIVERY_LIFECYCLE = _counter(
    "webhook_delivery_lifecycle_total",
    "Delivery lifecycle transitions",
    "runtime_role",
    "action",
    "outcome",
)
ATTEMPT_DURATION = _histogram(
    "webhook_delivery_attempt_duration_seconds",
    "Outbound attempt duration",
    "runtime_role",
    "outcome",
)
END_TO_END = _histogram(
    "webhook_delivery_end_to_end_seconds",
    "Acceptance-to-terminal duration",
    "runtime_role",
    "outcome",
)
ATTEMPTS_PER_SUCCESS = _histogram(
    "webhook_delivery_attempts_per_success",
    "Attempts required for success",
    "runtime_role",
    buckets=(1, 2, 3, 4, 5, 8, 13, 21, float("inf")),
)

OUTBOUND_RESPONSES = _counter(
    "webhook_outbound_responses_total",
    "Receiver HTTP response classes",
    "runtime_role",
    "status_class",
)
DELIVERY_FAILURES = _counter(
    "webhook_delivery_failures_total",
    "Sanitized delivery failures",
    "runtime_role",
    "failure_class",
)
CIRCUIT_ENDPOINTS = _gauge(
    "webhook_circuit_endpoints",
    "Endpoints grouped by circuit state",
    "runtime_role",
    "state",
)
WORKER_IN_FLIGHT = _gauge(
    "webhook_worker_in_flight",
    "Current worker in-flight deliveries",
    "runtime_role",
)
WORKER_UTILIZATION = _gauge(
    "webhook_worker_utilization_ratio",
    "Current worker slot utilization",
    "runtime_role",
)
DB_QUERY_DURATION = _histogram(
    "webhook_db_query_duration_seconds",
    "Database query duration",
    "runtime_role",
    "operation",
    "outcome",
)
DB_POOL_CONNECTIONS = _gauge(
    "webhook_db_pool_connections",
    "Database pool connections by state",
    "runtime_role",
    "state",
)
DB_POOL_ACQUISITION = _histogram(
    "webhook_db_pool_acquisition_seconds",
    "Database connection acquisition",
    "runtime_role",
    "outcome",
)
COLLECTION_FAILURES = _counter(
    "webhook_observability_collection_failures_total",
    "Metrics collection failures",
    "runtime_role",
    "collector",
)
SECURITY_DENIES = _gauge(
    "webhook_security_denies",
    "Process-local egress denials",
    "runtime_role",
    "layer",
    "reason",
)


def set_runtime_role(role: str) -> None:
    global _runtime_role
    if role not in {"api", "worker"}:
        raise ValueError("runtime role must be api or worker")
    _runtime_role = role


def status_class(status_code: int | None) -> str:
    if status_code is None:
        return "none"
    return f"{status_code // 100}xx" if 100 <= status_code <= 599 else "other"


def normalized_route(request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def record_http(method: str, route: str, code: int, elapsed: float) -> None:
    method = (
        method
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
        else "OTHER"
    )
    HTTP_REQUESTS.labels(
        _runtime_role, method, route, status_class(code)
    ).inc()
    HTTP_DURATION.labels(_runtime_role, method, route).observe(elapsed)


def record_event(outcome: str, reason: str = "none") -> None:
    EVENTS.labels(_runtime_role, outcome, reason).inc()


def record_enqueue(count: int) -> None:
    DELIVERY_LIFECYCLE.labels(_runtime_role, "enqueue", "accepted").inc(count)


def classify_failure(error: str | None) -> str:
    if error is None:
        return "none"
    value = error.lower()
    if "dns" in value:
        return "dns"
    if "tls" in value or "certificate" in value or "connecterror" in value:
        return "tls_or_connect"
    if "timeout" in value:
        return "timeout"
    if "proxy" in value or "unsafe_webhook" in value:
        return "policy_denied"
    if "http" in value:
        return "http"
    if "max_delivery_age" in value:
        return "max_age"
    return "other"


def record_claim(count: int, reclaimed: int = 0) -> None:
    DELIVERY_LIFECYCLE.labels(_runtime_role, "claim", "new").inc(
        count - reclaimed
    )
    if reclaimed:
        DELIVERY_LIFECYCLE.labels(_runtime_role, "claim", "lease_expired").inc(
            reclaimed
        )


def record_lease(outcome: str) -> None:
    DELIVERY_LIFECYCLE.labels(_runtime_role, "lease", outcome).inc()


def record_finalization(
    outcome: str,
    attempt_seconds: float,
    end_to_end_seconds: float,
    attempts: int,
    status_code: int | None,
    error: str | None,
) -> None:
    DELIVERY_LIFECYCLE.labels(_runtime_role, "completion", outcome).inc()
    ATTEMPT_DURATION.labels(_runtime_role, outcome).observe(
        max(0.0, attempt_seconds)
    )
    if outcome in {"succeeded", "dead"}:
        END_TO_END.labels(_runtime_role, outcome).observe(
            max(0.0, end_to_end_seconds)
        )
    if outcome == "succeeded":
        ATTEMPTS_PER_SUCCESS.labels(_runtime_role).observe(attempts)
    if status_code is not None:
        OUTBOUND_RESPONSES.labels(
            _runtime_role, status_class(status_code)
        ).inc()
    if error is not None:
        DELIVERY_FAILURES.labels(_runtime_role, classify_failure(error)).inc()


def record_stale_finalization() -> None:
    DELIVERY_LIFECYCLE.labels(_runtime_role, "finalize", "stale").inc()


def set_worker_in_flight(count: int, capacity: int) -> None:
    WORKER_IN_FLIGHT.labels(_runtime_role).set(count)
    WORKER_UTILIZATION.labels(_runtime_role).set(count / max(1, capacity))


def observe_pool_acquisition(elapsed: float, outcome: str = "success") -> None:
    DB_POOL_ACQUISITION.labels(_runtime_role, outcome).observe(
        max(0.0, elapsed)
    )


def current_trace_headers() -> tuple[str | None, str | None]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent"), carrier.get("tracestate")


@contextmanager
def delivery_span(claim: Any) -> Iterator[trace.Span]:
    carrier = {
        key: value
        for key, value in {
            "traceparent": claim.traceparent,
            "tracestate": claim.tracestate,
        }.items()
        if value
    }
    parent = propagate.extract(carrier)
    tracer = trace.get_tracer("app.delivery")
    with tracer.start_as_current_span(
        "webhook.delivery",
        context=parent,
        attributes={
            "webhook.delivery.id": claim.public_id,
            "webhook.event.id": claim.event_public_id,
            "webhook.attempt": claim.attempt_number,
        },
    ) as span:
        yield span


def configure_tracing(settings: Any, role: str) -> TracerProvider | None:
    """Configure one bounded sampler and optional OTLP/HTTP exporter."""
    global _provider
    set_runtime_role(role)
    if not settings.observability_enabled or not settings.tracing_enabled:
        return None
    if _provider is not None:
        return _provider
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.app_version,
            "service.instance.role": role,
            "deployment.environment.name": settings.environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
    )
    if settings.otel_exporter_otlp_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint.rstrip("/")
            + "/v1/traces"
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def instrument_fastapi(app: Any, settings: Any) -> None:
    if not settings.observability_enabled or not settings.tracing_enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=configure_tracing(settings, "api"),
        excluded_urls=(
            f"{re.escape(settings.metrics_path.lstrip('/'))},livez,readyz"
        ),
    )


def flush_tracing() -> None:
    if _provider is not None:
        _provider.force_flush(timeout_millis=5_000)


def _db_operation(statement: str) -> str:
    operation = (
        statement.lstrip().split(None, 1)[0].upper()
        if statement.strip()
        else "OTHER"
    )
    return (
        operation
        if operation in {"SELECT", "INSERT", "UPDATE", "DELETE", "DDL"}
        else "OTHER"
    )


def instrument_engine(engine: AsyncEngine, role: str) -> None:
    """Attach fixed-label SQL timing and pool-use hooks exactly once."""
    sync_engine = engine.sync_engine
    identity = id(sync_engine)
    if identity in _instrumented_engines:
        return
    _instrumented_engines.add(identity)

    @event.listens_for(sync_engine, "before_cursor_execute")
    def before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        del conn, cursor, parameters, executemany
        context._webhook_observation = (
            time.perf_counter(),
            _db_operation(statement),
        )

    @event.listens_for(sync_engine, "after_cursor_execute")
    def after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        del conn, cursor, statement, parameters, executemany
        started, operation = context._webhook_observation
        DB_QUERY_DURATION.labels(role, operation, "success").observe(
            time.perf_counter() - started
        )

    @event.listens_for(sync_engine, "handle_error")
    def handle_error(exception_context):
        context = exception_context.execution_context
        observed = getattr(context, "_webhook_observation", None)
        if observed:
            started, operation = observed
            DB_QUERY_DURATION.labels(role, operation, "error").observe(
                time.perf_counter() - started
            )

    @event.listens_for(sync_engine.pool, "checkout")
    def checkout(dbapi_connection, connection_record, connection_proxy):
        del dbapi_connection, connection_record, connection_proxy
        DB_POOL_CONNECTIONS.labels(role, "checked_out").inc()

    @event.listens_for(sync_engine.pool, "checkin")
    def checkin(dbapi_connection, connection_record):
        del dbapi_connection, connection_record
        DB_POOL_CONNECTIONS.labels(role, "checked_out").dec()


class QueueMetricsCollector:
    """Periodically replace gauges from authoritative database queries."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        interval: float,
        role: str,
    ):
        self.session_factory = session_factory
        self.interval = interval
        self.role = role
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            await self.collect_once()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self.interval)
            except TimeoutError:
                try:
                    await self.collect_once()
                except Exception:
                    COLLECTION_FAILURES.labels(self.role, "queue").inc()
                    logger.exception("authoritative_queue_metrics_failed")

    async def collect_once(self) -> None:
        from app.models import Delivery, EndpointQuotaState
        from app.security_observability import security_deny_counts

        now = datetime.now(timezone.utc)
        due = or_(
            and_(
                Delivery.status.in_(("pending", "retry_scheduled")),
                Delivery.next_attempt_at <= now,
            ),
            and_(
                Delivery.status == "processing",
                Delivery.lease_expires_at <= now,
            ),
        )
        due_at = case(
            (Delivery.status == "processing", Delivery.lease_expires_at),
            else_=Delivery.next_attempt_at,
        )
        async with self.session_factory() as session:
            status_rows = list(
                await session.execute(
                    select(Delivery.status, func.count(Delivery.id)).group_by(
                        Delivery.status
                    )
                )
            )
            runnable = int(
                await session.scalar(
                    select(func.count(Delivery.id)).where(due)
                )
                or 0
            )
            oldest = await session.scalar(select(func.min(due_at)).where(due))
            circuit_rows = list(
                await session.execute(
                    select(
                        EndpointQuotaState.circuit_state,
                        func.count(EndpointQuotaState.endpoint_id),
                    ).group_by(EndpointQuotaState.circuit_state)
                )
            )
        counts = {str(state): int(count) for state, count in status_rows}
        active = sum(
            counts.get(state, 0)
            for state in ("pending", "processing", "retry_scheduled")
        )
        values = {
            "total": active,
            "runnable": runnable,
            "processing": counts.get("processing", 0),
            "retry_scheduled": counts.get("retry_scheduled", 0),
            "dead": counts.get("dead", 0),
        }
        for kind, value in values.items():
            QUEUE_DEPTH.labels(self.role, kind).set(value)
        age = (
            0.0
            if oldest is None
            else max(
                0.0,
                (
                    now
                    - (
                        oldest
                        if oldest.tzinfo
                        else oldest.replace(tzinfo=timezone.utc)
                    )
                ).total_seconds(),
            )
        )
        QUEUE_OLDEST.labels(self.role).set(age)
        circuit_counts = {
            str(state): int(count) for state, count in circuit_rows
        }
        for state in ("closed", "open", "half_open"):
            CIRCUIT_ENDPOINTS.labels(self.role, state).set(
                circuit_counts.get(state, 0)
            )
        for (layer, reason), count in security_deny_counts().items():
            SECURITY_DENIES.labels(self.role, layer, reason).set(count)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def record_circuit_transition(state: str) -> None:
    DELIVERY_LIFECYCLE.labels(_runtime_role, "circuit", state).inc()


def set_pool_capacity(role: str, capacity: int) -> None:
    DB_POOL_CONNECTIONS.labels(role, "capacity").set(capacity)
