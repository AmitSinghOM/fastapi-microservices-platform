"""Deployment-wide tenant and endpoint fair delivery claiming."""

# Legacy declarative models expose runtime values as Column[T] to mypy.
# mypy: disable-error-code="assignment,arg-type,index"

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.admission import (
    consume_tokens,
    ensure_endpoint_state,
    ensure_global_state,
    ensure_tenant_state,
)
from app.config import Settings
from app.models import (
    Delivery,
    EndpointQuotaState,
    GlobalControlState,
    TenantQuotaState,
)


def rotate_after(values: list[int], cursor: int | None) -> list[int]:
    if not values or cursor is None:
        return values
    for index, value in enumerate(values):
        if value > cursor:
            return values[index:] + values[:index]
    return values


async def fair_candidate_ids(
    session: AsyncSession,
    now: datetime,
    scan_limit: int,
    per_tenant_limit: int,
) -> list[int]:
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
    endpoint_rank = func.row_number().over(
        partition_by=(Delivery.organization_id, Delivery.endpoint_id),
        order_by=(Delivery.next_attempt_at, Delivery.id),
    )
    by_endpoint = (
        select(
            Delivery.id.label("delivery_id"),
            Delivery.organization_id.label("organization_id"),
            Delivery.endpoint_id.label("endpoint_id"),
            Delivery.next_attempt_at.label("next_attempt_at"),
            endpoint_rank.label("endpoint_rank"),
        )
        .where(due)
        .cte("due_by_endpoint")
    )
    tenant_rank = func.row_number().over(
        partition_by=by_endpoint.c.organization_id,
        order_by=(
            by_endpoint.c.endpoint_rank,
            by_endpoint.c.next_attempt_at,
            by_endpoint.c.delivery_id,
        ),
    )
    ranked = select(
        by_endpoint,
        tenant_rank.label("tenant_rank"),
    ).cte("due_by_tenant")
    rows = await session.execute(
        select(ranked.c.delivery_id)
        .where(ranked.c.tenant_rank <= per_tenant_limit)
        .order_by(
            ranked.c.tenant_rank,
            ranked.c.next_attempt_at,
            ranked.c.delivery_id,
        )
        .limit(scan_limit)
    )
    return list(rows.scalars())


async def select_fair_deliveries(
    session: AsyncSession,
    settings: Settings,
    now: datetime,
    requested: int,
) -> list[Delivery]:
    """Choose work within deployment, tenant, endpoint, and rate caps."""
    await ensure_global_state(session, now)
    global_state = await session.scalar(
        select(GlobalControlState)
        .where(GlobalControlState.id == 1)
        .with_for_update()
    )
    if global_state is None:
        raise RuntimeError("Global scheduler state is unavailable")

    live_filter = and_(
        Delivery.status == "processing",
        Delivery.lease_expires_at > now,
    )
    live_total = await session.scalar(
        select(func.count(Delivery.id)).where(live_filter)
    )
    available = min(
        requested,
        settings.worker_global_concurrency - int(live_total or 0),
    )
    if available <= 0:
        return []

    per_tenant_scan = max(available * 2, settings.endpoint_concurrency)
    candidate_ids = await fair_candidate_ids(
        session,
        now,
        settings.worker_candidate_scan_limit,
        per_tenant_scan,
    )
    if not candidate_ids:
        return []
    locked = list(
        await session.scalars(
            select(Delivery)
            .options(joinedload(Delivery.event))
            .where(Delivery.id.in_(candidate_ids))
            .with_for_update(of=Delivery, skip_locked=True)
        )
    )
    by_id = {delivery.id: delivery for delivery in locked}
    candidates = [
        by_id[delivery_id]
        for delivery_id in candidate_ids
        if delivery_id in by_id
    ]
    if not candidates:
        return []

    organization_ids = sorted(
        {delivery.organization_id for delivery in candidates}
    )
    endpoint_ids = sorted({delivery.endpoint_id for delivery in candidates})
    for organization_id in organization_ids:
        await ensure_tenant_state(
            session, organization_id, settings, now
        )
    for endpoint_id in endpoint_ids:
        await ensure_endpoint_state(session, endpoint_id, settings, now)

    tenant_states = list(
        await session.scalars(
            select(TenantQuotaState)
            .where(TenantQuotaState.organization_id.in_(organization_ids))
            .order_by(TenantQuotaState.organization_id)
            .with_for_update()
        )
    )
    endpoint_states = list(
        await session.scalars(
            select(EndpointQuotaState)
            .where(EndpointQuotaState.endpoint_id.in_(endpoint_ids))
            .order_by(EndpointQuotaState.endpoint_id)
            .with_for_update()
        )
    )
    tenant_state_by_id = {
        state.organization_id: state for state in tenant_states
    }
    endpoint_state_by_id = {
        state.endpoint_id: state for state in endpoint_states
    }

    tenant_counts = {
        organization_id: count
        for organization_id, count in (
            await session.execute(
                select(
                    Delivery.organization_id,
                    func.count(Delivery.id),
                )
                .where(live_filter)
                .group_by(Delivery.organization_id)
            )
        )
    }
    endpoint_counts = {
        endpoint_id: count
        for endpoint_id, count in (
            await session.execute(
                select(Delivery.endpoint_id, func.count(Delivery.id))
                .where(live_filter)
                .group_by(Delivery.endpoint_id)
            )
        )
    }

    endpoint_balances = {}
    for endpoint_id, endpoint_state in endpoint_state_by_id.items():
        balance = consume_tokens(
            endpoint_state.delivery_tokens,
            endpoint_state.refilled_at,
            settings.endpoint_rate_per_second,
            settings.endpoint_rate_burst,
            0,
            now,
            settings.quota_retry_after_max_seconds,
        )
        endpoint_state.delivery_tokens = balance.tokens
        endpoint_state.refilled_at = balance.refilled_at
        endpoint_state.updated_at = now
        endpoint_balances[endpoint_id] = balance.tokens

    grouped: dict[int, dict[int, deque[Delivery]]] = defaultdict(
        lambda: defaultdict(deque)
    )
    for delivery in candidates:
        grouped[delivery.organization_id][delivery.endpoint_id].append(
            delivery
        )
    organization_order = rotate_after(
        organization_ids,
        global_state.tenant_cursor_organization_id,
    )
    endpoint_order: dict[int, deque[int]] = {}
    for organization_id in organization_order:
        tenant_state = tenant_state_by_id[organization_id]
        endpoint_order[organization_id] = deque(
            rotate_after(
                sorted(grouped[organization_id]),
                tenant_state.endpoint_cursor_id,
            )
        )

    selected: list[Delivery] = []
    while len(selected) < available:
        made_progress = False
        for organization_id in organization_order:
            if len(selected) >= available:
                break
            if (
                int(tenant_counts.get(organization_id, 0))
                >= settings.tenant_in_flight_deliveries
            ):
                continue
            endpoints = endpoint_order[organization_id]
            for _ in range(len(endpoints)):
                endpoint_id = endpoints.popleft()
                endpoints.append(endpoint_id)
                queue = grouped[organization_id][endpoint_id]
                if not queue:
                    continue
                if (
                    int(endpoint_counts.get(endpoint_id, 0))
                    >= settings.endpoint_concurrency
                ):
                    continue
                if endpoint_balances[endpoint_id] < 1.0:
                    continue
                delivery = queue.popleft()
                selected.append(delivery)
                endpoint_balances[endpoint_id] -= 1.0
                endpoint_counts[endpoint_id] = (
                    int(endpoint_counts.get(endpoint_id, 0)) + 1
                )
                tenant_counts[organization_id] = (
                    int(tenant_counts.get(organization_id, 0)) + 1
                )
                endpoint_state_by_id[endpoint_id].delivery_tokens = (
                    endpoint_balances[endpoint_id]
                )
                tenant_state_by_id[organization_id].endpoint_cursor_id = (
                    endpoint_id
                )
                tenant_state_by_id[organization_id].updated_at = now
                global_state.tenant_cursor_organization_id = organization_id
                global_state.updated_at = now
                made_progress = True
                break
        if not made_progress:
            break
    return selected
