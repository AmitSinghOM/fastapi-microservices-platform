from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.exceptions import ForbiddenError, NotFoundError
from app.models import (
    AdministrativeAuditEvent,
    Delivery,
    Event,
    Project,
    ReplayOperation,
)
from app.principals import MachinePrincipal
from app.services.webhook_service import WebhookService
from app.tests.test_delivery_contracts import seed_delivery, worker_settings


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_machine_replay_audit_and_project_boundary(
    postgres_session_factory,
):
    delivery_id = await seed_delivery(postgres_session_factory)
    async with postgres_session_factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        event = await session.get(Event, delivery.event_id)
        project = await session.get(Project, event.project_id)
        delivery.status = "dead"
        await session.commit()
        actor = MachinePrincipal(
            str(uuid4()),
            project.id,
            frozenset({"deliveries:read", "deliveries:replay"}),
        )
        service = WebhookService(session, worker_settings())
        assessment = await service.assess_recovery(
            actor, project.public_id, delivery.public_id
        )
        assert assessment["eligible_for_replay"] is True
        with pytest.raises(NotFoundError):
            await service.list_deliveries(actor, str(uuid4()), 0, 50)
        with pytest.raises(ForbiddenError):
            await service.list_endpoints(actor, project.public_id, 0, 50)
        replay = await service.replay_deliveries(
            actor, project.public_id, [delivery.public_id], "machine-recovery"
        )
        repeated = await service.replay_deliveries(
            actor, project.public_id, [delivery.public_id], "machine-recovery"
        )
        assert repeated.public_id == replay.public_id
        assert replay.actor_user_id is None
        assert (
            await session.scalar(
                select(func.count()).select_from(ReplayOperation)
            )
            == 1
        )
        audit = await session.scalar(
            select(AdministrativeAuditEvent).where(
                AdministrativeAuditEvent.resource_public_id == replay.public_id
            )
        )
        assert audit.actor_user_id is None
        assert audit.sanitized_metadata["actor_id"] == actor.credential_id
        assert audit.sanitized_metadata["actor_type"] == "api_key"
        copied = await session.scalar(
            select(Delivery).where(
                Delivery.public_id == replay.created_delivery_ids[0]
            )
        )
        assert copied.event_id == delivery.event_id
        assert copied.endpoint_url_snapshot == delivery.endpoint_url_snapshot
        assert (
            copied.signing_secret_version_snapshot
            == delivery.signing_secret_version_snapshot
        )
