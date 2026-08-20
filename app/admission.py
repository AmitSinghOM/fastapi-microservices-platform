"""Replica-shared PostgreSQL admission controls and quota state."""

# Legacy declarative models expose runtime values as Column[T] to mypy.
# mypy: disable-error-code="assignment,arg-type"

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.exceptions import QuotaExceededError, SaturationError
from app.models import (
    Delivery,
    EndpointQuotaState,
    Event,
    GlobalControlState,
    Project,
    TenantQuotaState,
    WebhookEndpoint,
)

NONTERMINAL_STATUSES = ("pending", "processing", "retry_scheduled")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def database_now(session: AsyncSession) -> datetime:
    if session.get_bind().dialect.name == "sqlite":
        return utcnow()
    value = await session.scalar(select(func.now()))
    if value is None:
        raise RuntimeError("Database did not return its current time")
    return aware(value)


def tenant_quota_values(
    organization_id: int, settings: Settings, now: datetime
) -> dict[str, object]:
    return {
        "organization_id": organization_id,
        "event_tokens": float(settings.tenant_event_burst),
        "event_refilled_at": now,
        "delivery_tokens": float(settings.tenant_delivery_burst),
        "delivery_refilled_at": now,
        "replay_tokens": float(settings.tenant_replay_burst),
        "replay_refilled_at": now,
        "endpoint_cursor_id": None,
        "updated_at": now,
    }


def endpoint_quota_values(
    endpoint_id: int, settings: Settings, now: datetime
) -> dict[str, object]:
    return {
        "endpoint_id": endpoint_id,
        "delivery_tokens": float(settings.endpoint_rate_burst),
        "refilled_at": now,
        "updated_at": now,
    }


async def _insert_do_nothing(
    session: AsyncSession,
    model: type,
    values: dict[str, object],
    key: str,
) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = pg_insert(model).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(model).values(**values)
    else:
        raise RuntimeError(f"Unsupported admission dialect: {dialect}")
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=[key])
    )


async def ensure_global_state(
    session: AsyncSession, now: datetime
) -> None:
    await _insert_do_nothing(
        session,
        GlobalControlState,
        {
            "id": 1,
            "tenant_cursor_organization_id": None,
            "created_at": now,
            "updated_at": now,
        },
        "id",
    )


async def ensure_tenant_state(
    session: AsyncSession,
    organization_id: int,
    settings: Settings,
    now: datetime,
) -> None:
    await _insert_do_nothing(
        session,
        TenantQuotaState,
        tenant_quota_values(organization_id, settings, now),
        "organization_id",
    )


async def ensure_endpoint_state(
    session: AsyncSession,
    endpoint_id: int,
    settings: Settings,
    now: datetime,
) -> None:
    await _insert_do_nothing(
        session,
        EndpointQuotaState,
        endpoint_quota_values(endpoint_id, settings, now),
        "endpoint_id",
    )


@dataclass(frozen=True)
class TokenBalance:
    tokens: float
    refilled_at: datetime
    retry_after: int | None


def consume_tokens(
    tokens: float,
    refilled_at: datetime,
    rate: float,
    capacity: int,
    cost: int,
    now: datetime,
    retry_cap: int,
) -> TokenBalance:
    elapsed = max(0.0, (now - aware(refilled_at)).total_seconds())
    available = min(float(capacity), float(tokens) + elapsed * rate)
    if available >= cost:
        return TokenBalance(available - cost, now, None)
    wait = math.ceil((cost - available) / rate)
    return TokenBalance(
        available,
        now,
        min(retry_cap, max(1, wait)),
    )


class AdmissionController:
    """Serialize short global/tenant checks and consume shared tokens."""

    def __init__(self, session: AsyncSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def lock_global_tenant(
        self, organization_id: int
    ) -> tuple[GlobalControlState, TenantQuotaState, datetime]:
        now = await database_now(self.session)
        await ensure_global_state(self.session, now)
        await ensure_tenant_state(
            self.session, organization_id, self.settings, now
        )
        global_state = await self.session.scalar(
            select(GlobalControlState)
            .where(GlobalControlState.id == 1)
            .with_for_update()
        )
        tenant_state = await self.session.scalar(
            select(TenantQuotaState)
            .where(TenantQuotaState.organization_id == organization_id)
            .with_for_update()
        )
        if global_state is None or tenant_state is None:
            raise RuntimeError("Admission control state is unavailable")
        return global_state, tenant_state, now

    async def check_saturation(
        self, now: datetime, additional_deliveries: int
    ) -> None:
        backlog = await self.session.scalar(
            select(func.count(Delivery.id)).where(
                Delivery.status.in_(NONTERMINAL_STATUSES)
            )
        )
        if (
            int(backlog or 0) + additional_deliveries
            > self.settings.global_max_backlog
        ):
            raise SaturationError("global_backlog")
        due_at = case(
            (
                and_(
                    Delivery.status.in_(("pending", "retry_scheduled")),
                    Delivery.next_attempt_at <= now,
                ),
                Delivery.next_attempt_at,
            ),
            (
                and_(
                    Delivery.status == "processing",
                    Delivery.lease_expires_at <= now,
                ),
                Delivery.lease_expires_at,
            ),
            else_=None,
        )
        oldest_due = await self.session.scalar(
            select(func.min(due_at)).where(
                or_(
                    and_(
                        Delivery.status.in_(
                            ("pending", "retry_scheduled")
                        ),
                        Delivery.next_attempt_at <= now,
                    ),
                    and_(
                        Delivery.status == "processing",
                        Delivery.lease_expires_at <= now,
                    ),
                )
            )
        )
        if oldest_due is not None:
            age = (now - aware(oldest_due)).total_seconds()
            if age >= self.settings.global_oldest_due_admission_seconds:
                raise SaturationError("oldest_due_age")

    def _event_balance(
        self, state: TenantQuotaState, now: datetime
    ) -> TokenBalance:
        return consume_tokens(
            state.event_tokens,
            state.event_refilled_at,
            self.settings.tenant_event_rate_per_second,
            self.settings.tenant_event_burst,
            1,
            now,
            self.settings.quota_retry_after_max_seconds,
        )

    def _delivery_balance(
        self, state: TenantQuotaState, count: int, now: datetime
    ) -> TokenBalance:
        return consume_tokens(
            state.delivery_tokens,
            state.delivery_refilled_at,
            self.settings.tenant_delivery_rate_per_second,
            self.settings.tenant_delivery_burst,
            count,
            now,
            self.settings.quota_retry_after_max_seconds,
        )

    async def admit_event(
        self,
        organization_id: int,
        delivery_count: int,
        envelope_size: int,
    ) -> datetime:
        _, state, now = await self.lock_global_tenant(organization_id)
        await self.admit_event_locked(
            state,
            now,
            organization_id,
            delivery_count,
            envelope_size,
        )
        return now

    async def admit_event_locked(
        self,
        state: TenantQuotaState,
        now: datetime,
        organization_id: int,
        delivery_count: int,
        envelope_size: int,
    ) -> None:
        if delivery_count > self.settings.tenant_fanout_per_event:
            raise QuotaExceededError(
                "fanout_per_event",
                self.settings.quota_retry_after_max_seconds,
            )
        await self.check_saturation(now, delivery_count)
        retained = await self.session.scalar(
            select(
                func.coalesce(
                    func.sum(func.length(Event.canonical_envelope)), 0
                )
            )
            .join(Project, Event.project_id == Project.id)
            .where(Project.organization_id == organization_id)
        )
        if (
            int(retained or 0) + envelope_size
            > self.settings.tenant_retained_bytes
        ):
            raise QuotaExceededError(
                "retained_bytes",
                self.settings.quota_retry_after_max_seconds,
            )
        event_balance = self._event_balance(state, now)
        delivery_balance = self._delivery_balance(state, delivery_count, now)
        if event_balance.retry_after is not None:
            raise QuotaExceededError(
                "event_rate", event_balance.retry_after
            )
        if delivery_balance.retry_after is not None:
            raise QuotaExceededError(
                "delivery_rate", delivery_balance.retry_after
            )
        state.event_tokens = event_balance.tokens
        state.event_refilled_at = event_balance.refilled_at
        state.delivery_tokens = delivery_balance.tokens
        state.delivery_refilled_at = delivery_balance.refilled_at
        state.updated_at = now

    async def admit_replay(self, organization_id: int) -> datetime:
        _, state, now = await self.lock_global_tenant(organization_id)
        await self.check_saturation(now, 1)
        replay_balance = consume_tokens(
            state.replay_tokens,
            state.replay_refilled_at,
            self.settings.tenant_replay_rate_per_second,
            self.settings.tenant_replay_burst,
            1,
            now,
            self.settings.quota_retry_after_max_seconds,
        )
        delivery_balance = self._delivery_balance(state, 1, now)
        if replay_balance.retry_after is not None:
            raise QuotaExceededError(
                "replay_rate", replay_balance.retry_after
            )
        if delivery_balance.retry_after is not None:
            raise QuotaExceededError(
                "delivery_rate", delivery_balance.retry_after
            )
        state.replay_tokens = replay_balance.tokens
        state.replay_refilled_at = replay_balance.refilled_at
        state.delivery_tokens = delivery_balance.tokens
        state.delivery_refilled_at = delivery_balance.refilled_at
        state.updated_at = now
        return now

    async def check_endpoint_limit(
        self,
        organization_id: int,
        project_id: int,
        exclude_endpoint_id: int | None = None,
    ) -> datetime:
        now = await database_now(self.session)
        await ensure_tenant_state(
            self.session, organization_id, self.settings, now
        )
        state = await self.session.scalar(
            select(TenantQuotaState)
            .where(TenantQuotaState.organization_id == organization_id)
            .with_for_update()
        )
        if state is None:
            raise RuntimeError("Tenant quota state is unavailable")
        endpoint_count_query = select(
            func.count(WebhookEndpoint.id)
        ).where(
            WebhookEndpoint.project_id == project_id,
            WebhookEndpoint.is_active.is_(True),
        )
        if exclude_endpoint_id is not None:
            endpoint_count_query = endpoint_count_query.where(
                WebhookEndpoint.id != exclude_endpoint_id
            )
        endpoint_count = await self.session.scalar(endpoint_count_query)
        if (
            int(endpoint_count or 0)
            >= self.settings.tenant_endpoints_per_project
        ):
            raise QuotaExceededError(
                "endpoints_per_project",
                self.settings.quota_retry_after_max_seconds,
            )
        return now
