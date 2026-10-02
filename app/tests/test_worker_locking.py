import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.scheduling as scheduling
import app.services.delivery_service as delivery_module
from app.models import Delivery, DeliveryAttempt, EndpointQuotaState
from app.services.delivery_service import DeliveryService, database_now
from app.tests.test_delivery_contracts import (
    finalize_success,
    seed_delivery,
    worker_settings,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "changed_state", ["canceled", "succeeded", "renewed", "deferred"]
)
async def test_claim_rechecks_candidates_after_scan(
    postgres_session_factory, monkeypatch, changed_state
):
    factory = postgres_session_factory
    delivery_id = await seed_delivery(factory)
    scan = scheduling.fair_candidate_ids

    async def change_after_scan(session, now, scan_limit, per_tenant_limit):
        ids = await scan(session, now, scan_limit, per_tenant_limit)
        assert ids == [delivery_id]
        values = {"status": changed_state}
        if changed_state == "renewed":
            values = {
                "status": "processing",
                "lease_token": "other-owner",
                "lease_expires_at": now + timedelta(minutes=1),
            }
        elif changed_state == "deferred":
            values = {
                "status": "retry_scheduled",
                "next_attempt_at": now + timedelta(minutes=1),
            }
        async with factory() as other:
            async with other.begin():
                await other.execute(
                    update(Delivery)
                    .where(Delivery.id == delivery_id)
                    .values(**values)
                )
        return ids

    monkeypatch.setattr(scheduling, "fair_candidate_ids", change_after_scan)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(factory, client, worker_settings())
        assert await service.claim_due(1) == []
    async with factory() as session:
        delivery = await session.get(Delivery, delivery_id)
        assert delivery.attempt_count == 0
        expected_status = {
            "renewed": "processing",
            "deferred": "retry_scheduled",
        }.get(changed_state, changed_state)
        assert delivery.status == expected_status


@pytest.mark.parametrize(
    "operation", ["renew", "finalize", "finalize_endpoint"]
)
async def test_expiry_is_rechecked_after_lock_wait(
    postgres_session_factory, monkeypatch, operation
):
    factory = postgres_session_factory
    delivery_id = await seed_delivery(factory)
    async with httpx.AsyncClient() as client:
        service = DeliveryService(factory, client, worker_settings())
        claim = (await service.claim_due(1))[0]
        async with factory() as session:
            delivery = await session.get(Delivery, delivery_id)
            expires = delivery.lease_expires_at
        before_expiry = expires - timedelta(seconds=1)
        after_expiry = expires + timedelta(seconds=1)
        clock = before_expiry
        waiting = asyncio.Event()
        lock_entity = (
            EndpointQuotaState
            if operation == "finalize_endpoint"
            else Delivery
        )

        class ObservedSession(AsyncSession):
            async def scalar(self, statement, *args, **kwargs):
                if (
                    statement._for_update_arg is not None
                    and statement.column_descriptions[0].get("entity")
                    is lock_entity
                ):
                    waiting.set()
                return await super().scalar(statement, *args, **kwargs)

        async def controlled_clock(session):
            return clock

        monkeypatch.setattr(delivery_module, "database_now", controlled_clock)
        observed = async_sessionmaker(
            factory.kw["bind"], class_=ObservedSession, expire_on_commit=False
        )
        service = DeliveryService(observed, client, worker_settings())
        async with factory() as blocker:
            async with blocker.begin():
                predicate = (
                    EndpointQuotaState.endpoint_id == claim.endpoint_id
                    if lock_entity is EndpointQuotaState
                    else Delivery.id == delivery_id
                )
                await blocker.scalar(
                    select(lock_entity).where(predicate).with_for_update()
                )
                task = asyncio.create_task(
                    service.renew_lease(claim)
                    if operation == "renew"
                    else finalize_success(service, claim)
                )
                await asyncio.wait_for(waiting.wait(), timeout=2)
                clock = after_expiry
            assert await asyncio.wait_for(task, timeout=2) is False
        async with factory() as session:
            assert (
                await session.scalar(select(func.count(DeliveryAttempt.id)))
                == 0
            )
            stored = await session.get(Delivery, delivery_id)
            assert stored.lease_expires_at == expires
            assert stored.status == "processing"


async def test_lease_clock_is_not_frozen_at_transaction_start(
    postgres_session_factory,
):
    async with postgres_session_factory() as session:
        async with session.begin():
            transaction_time = await session.scalar(select(func.now()))
            wall_time = await database_now(session)
            assert wall_time > transaction_time
