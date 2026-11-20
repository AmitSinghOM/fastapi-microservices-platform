import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select

import app.main as main_module
import app.middleware as middleware_module
from app.config import Settings
from app.exceptions import ConflictError, ForbiddenError, ValidationError
from app.middleware import BodySizeLimitMiddleware
from app.models import (
    AdministrativeAuditEvent,
    Delivery,
    Event,
    Organization,
    OrganizationMember,
    Project,
    User,
)
from app.principals import MachinePrincipal
from app.services.webhook_service import WebhookService
from app.tests.test_delivery_contracts import (
    now,
    seed_delivery,
    worker_settings,
)
from app.tests.test_phase3_ssrf import deployed_settings


@pytest.mark.parametrize("declared", [None, b"0", b"invalid"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_stream_limit_stops_before_downstream_parsing(
    monkeypatch, declared, overflow
):
    settings = worker_settings().model_copy(
        update={"webhook_payload_max_bytes": 1}
    )
    monkeypatch.setattr(middleware_module, "get_settings", lambda: settings)
    limit = 65_537
    reads = 0
    forwarded = []
    responses = []

    async def receive():
        nonlocal reads
        reads += 1
        if reads == 1:
            return {
                "type": "http.request",
                "body": b"a" * limit,
                "more_body": overflow,
            }
        if reads == 2:
            return {"type": "http.request", "body": b"x", "more_body": True}
        pytest.fail("Read past body limit")

    async def downstream(scope, receive, send):
        forwarded.append(await receive())

    async def send(message):
        responses.append(message)

    headers = [] if declared is None else [(b"content-length", declared)]
    await BodySizeLimitMiddleware(downstream)(
        {"type": "http", "headers": headers}, receive, send
    )
    if overflow:
        assert reads == 2
        assert not forwarded
        assert responses[0]["status"] == 413
    else:
        assert reads == 1
        assert len(forwarded[0]["body"]) == limit


async def test_oversized_chunked_api_request_returns_413(client):
    async def chunks():
        yield b"x" * 400_000
        pytest.fail("Oversized request was drained instead of rejected")

    response = await client.post("/v1/events", content=chunks())
    assert response.status_code == 413


async def test_metrics_requires_dedicated_bearer(monkeypatch):
    token = "monitoring-only-token-" + "x" * 32
    settings = worker_settings().model_copy(
        update={"metrics_bearer_token": token}
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = main_module.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for authorization in ("", "Bearer wrong", "Basic " + token):
            response = await client.get(
                "/metrics", headers={"Authorization": authorization}
            )
            assert response.status_code == 401
            assert token not in response.text
        response = await client.get(
            "/metrics", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 200
        assert token not in response.text


def test_production_requires_metrics_secret_and_postgres():
    with pytest.raises(ValueError, match="METRICS_BEARER_TOKEN"):
        deployed_settings(metrics_bearer_token="")
    with pytest.raises(ValueError, match="PostgreSQL"):
        deployed_settings(database_url="sqlite+aiosqlite:///:memory:")
    assert deployed_settings().metrics_bearer_token not in repr(
        deployed_settings()
    )


@pytest.mark.parametrize(
    "replicas", ["api_replica_count", "worker_replica_count"]
)
def test_sqlite_replica_contract(replicas):
    with pytest.raises(ValueError, match="SQLite supports"):
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            database_connection_budget=100,
            **{replicas: 2},
        )


def test_unsupported_database_is_rejected():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(
            _env_file=None, database_url="mysql+aiomysql://localhost/test"
        )


async def seed_user(factory):
    async with factory() as session:
        user = User(
            email=f"{uuid4()}@example.com",
            name="Owner",
            hashed_password="unused",
            created_at=now(),
        )
        session.add(user)
        await session.commit()
        return user.id


async def exercise_creation_caps(factory):
    user_id = await seed_user(factory)
    other_id = await seed_user(factory)
    settings = worker_settings().model_copy(
        update={
            "max_organizations_per_user": 1,
            "max_projects_per_organization": 1,
        }
    )

    async def create_org():
        async with factory() as session:
            try:
                return await WebhookService(
                    session, settings
                ).create_organization(user_id, str(uuid4()))
            except ForbiddenError:
                return None

    organizations = await asyncio.gather(create_org(), create_org())
    assert sum(org is not None for org in organizations) == 1
    organization = next(org for org in organizations if org is not None)

    async def create_project():
        async with factory() as session:
            try:
                return await WebhookService(session, settings).create_project(
                    user_id, organization.public_id, str(uuid4())
                )
            except ForbiddenError:
                return None

    projects = await asyncio.gather(create_project(), create_project())
    assert sum(project is not None for project in projects) == 1
    async with factory() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Project))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(Organization)
            )
            == 1
        )
        audit = list(await session.scalars(select(AdministrativeAuditEvent)))
        assert sorted(row.action for row in audit) == [
            "organization.created",
            "project.created",
        ]
        # Ownership transfer must not replenish a creator's fresh-tenant budget.
        member = await session.scalar(select(OrganizationMember))
        member.user_id = other_id
        await session.commit()
        with pytest.raises(ForbiddenError):
            await WebhookService(session, settings).create_organization(
                user_id, "bypass"
            )
        await session.rollback()
        other = await WebhookService(session, settings).create_organization(
            other_id, "independent"
        )
        assert other.public_id != organization.public_id
        await session.execute(
            delete(Project).where(Project.organization_id == organization.id)
        )
        await session.execute(
            delete(Organization).where(Organization.id == organization.id)
        )
        await session.commit()
        with pytest.raises(ForbiddenError):
            await WebhookService(session, settings).create_organization(
                user_id, "deleted-budget"
            )


async def test_sqlite_creation_caps_are_serialized(
    db_session, sqlite_session_factory
):
    await exercise_creation_caps(sqlite_session_factory)


@pytest.mark.postgres
async def test_postgres_creation_caps_are_serialized(postgres_session_factory):
    await exercise_creation_caps(postgres_session_factory)


async def test_replay_same_set_is_idempotent_and_duplicates_fail(
    db_session, sqlite_session_factory
):
    delivery_id = await seed_delivery(sqlite_session_factory)
    delivery = await db_session.get(Delivery, delivery_id)
    event = await db_session.get(Event, delivery.event_id)
    project = await db_session.get(Project, event.project_id)
    second = WebhookService._replay_copy(delivery, now())
    delivery.status = second.status = "dead"
    db_session.add(second)
    await db_session.commit()
    actor = MachinePrincipal(
        str(uuid4()), project.id, frozenset({"deliveries:replay"})
    )
    service = WebhookService(db_session, worker_settings())
    sources = [delivery.public_id, second.public_id]
    operation = await service.replay_deliveries(
        actor, project.public_id, sources, "batch"
    )
    repeated = await service.replay_deliveries(
        actor, project.public_id, sources[::-1], "batch"
    )
    assert operation.public_id == repeated.public_id
    assert repeated.source_delivery_ids == sources
    with pytest.raises(ConflictError):
        await service.replay_deliveries(
            actor, project.public_id, sources[:1], "batch"
        )
    with pytest.raises(ValidationError, match="unique"):
        await service.replay_deliveries(
            actor, project.public_id, [sources[0]] * 2, "batch"
        )


async def test_body_guard_passes_lifespan_and_stops_on_disconnect():
    forwarded = []

    async def downstream(scope, receive, send):
        forwarded.append(scope["type"])

    async def disconnected():
        return {"type": "http.disconnect"}

    async def send(message):
        pytest.fail("Disconnected client must not receive a response")

    middleware = BodySizeLimitMiddleware(downstream)
    await middleware({"type": "lifespan"}, disconnected, send)
    await middleware({"type": "http", "headers": []}, disconnected, send)
    assert forwarded == ["lifespan"]


async def test_organization_creation_route_is_throttled(
    client, auth, monkeypatch
):
    monkeypatch.setattr(
        middleware_module.RateLimitMiddleware,
        "_check",
        lambda *args: (False, 5),
    )
    response = await client.post(
        "/v1/organizations", headers=auth[1], json={"name": "refused"}
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_token_must_be_present_and_long(environment):
    for token in ("", "short"):
        with pytest.raises(ValueError, match="METRICS_BEARER_TOKEN"):
            deployed_settings(
                environment=environment, metrics_bearer_token=token
            )


async def test_request_body_has_total_deadline(monkeypatch):
    settings = worker_settings().model_copy(
        update={"request_body_timeout_seconds": 0.001}
    )
    monkeypatch.setattr(middleware_module, "get_settings", lambda: settings)
    messages = []

    async def stalled_receive():
        await asyncio.Event().wait()

    async def downstream(*args):
        pytest.fail("Timed out body reached parser")

    async def send(message):
        messages.append(message)

    await BodySizeLimitMiddleware(downstream)(
        {"type": "http", "headers": []}, stalled_receive, send
    )
    assert messages[0]["status"] == 408
