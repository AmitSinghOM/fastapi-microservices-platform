"""Bounded retention and organization lifecycle maintenance."""

# Legacy declarative models expose runtime values as Column[T] to mypy.
# mypy: disable-error-code="assignment,arg-type"

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    String,
    and_,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import append_audit
from app.config import Settings
from app.models import (
    Delivery,
    DeliveryAttempt,
    Event,
    LoginThrottle,
    Organization,
    OrganizationLifecycleOperation,
    OrganizationPolicy,
    Project,
)

logger = logging.getLogger(__name__)

_TERMINAL_DELIVERY_STATUSES = ("succeeded", "dead", "canceled")
_QUEUED_DELIVERY_STATUSES = ("pending", "retry_scheduled")


@dataclass(frozen=True)
class LifecycleRunResult:
    responses_purged: int = 0
    payloads_purged: int = 0
    deliveries_canceled: int = 0
    deliveries_deleted: int = 0
    events_deleted: int = 0
    organizations_deleted: int = 0
    login_throttles_purged: int = 0

    def merged(self, other: "LifecycleRunResult") -> "LifecycleRunResult":
        return LifecycleRunResult(
            responses_purged=(self.responses_purged + other.responses_purged),
            payloads_purged=self.payloads_purged + other.payloads_purged,
            deliveries_canceled=(
                self.deliveries_canceled + other.deliveries_canceled
            ),
            deliveries_deleted=(
                self.deliveries_deleted + other.deliveries_deleted
            ),
            events_deleted=self.events_deleted + other.events_deleted,
            organizations_deleted=(
                self.organizations_deleted + other.organizations_deleted
            ),
            login_throttles_purged=(
                self.login_throttles_purged + other.login_throttles_purged
            ),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.current_timestamp()))
    if value is None:
        raise RuntimeError("Database did not return its current time")
    return _aware(value)


def _expired_by_policy(
    session: AsyncSession,
    timestamp_column: Any,
    retention_days_column: Any,
) -> Any:
    if session.get_bind().dialect.name == "sqlite":
        modifier = (
            literal("-")
            + cast(retention_days_column, String)
            + literal(" days")
        )
        return func.datetime(timestamp_column) <= func.datetime(
            func.current_timestamp(), modifier
        )
    return timestamp_column <= func.now() - func.make_interval(
        0, 0, 0, retention_days_column
    )


def _claim_rows(
    statement: Any,
    session: AsyncSession,
    entity: type[Any],
) -> Any:
    if session.get_bind().dialect.name == "postgresql":
        return statement.with_for_update(skip_locked=True, of=entity)
    return statement


class LifecycleService:
    """Run short, bounded maintenance transactions safely per worker."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def run_once(self) -> LifecycleRunResult:
        """Run each maintenance category in an independent transaction."""
        result = await self._purge_responses()
        result = result.merged(await self._purge_event_payloads())
        result = result.merged(await self._purge_stale_login_throttles())
        return result.merged(await self._cleanup_due_organization())

    async def _purge_stale_login_throttles(self) -> LifecycleRunResult:
        """Delete throttle rows whose window and lock have both lapsed.

        Failed logins against arbitrary addresses each create a row, so
        without this sweep an unauthenticated caller grows the table
        without bound. A row is inert — and safe to delete — once its
        failure window has fully elapsed and no lock is active; recreating
        it later is exactly the fresh-window behavior.
        """
        async with self.session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                window_started_before = now - timedelta(
                    seconds=self.settings.login_throttle_window_seconds
                )
                rows = list(
                    await session.scalars(
                        _claim_rows(
                            select(LoginThrottle)
                            .where(
                                LoginThrottle.updated_at
                                <= window_started_before,
                                or_(
                                    LoginThrottle.locked_until.is_(None),
                                    LoginThrottle.locked_until <= now,
                                ),
                            )
                            .order_by(LoginThrottle.updated_at)
                            .limit(self.settings.lifecycle_cleanup_batch_size),
                            session,
                            LoginThrottle,
                        )
                    )
                )
                for row in rows:
                    await session.delete(row)
        return LifecycleRunResult(login_throttles_purged=len(rows))

    async def _purge_responses(self) -> LifecycleRunResult:
        async with self.session_factory() as session:
            async with session.begin():
                purged_at = await _database_now(session)
                statement = (
                    select(DeliveryAttempt)
                    .join(Delivery, Delivery.id == DeliveryAttempt.delivery_id)
                    .join(
                        OrganizationPolicy,
                        OrganizationPolicy.organization_id
                        == Delivery.organization_id,
                    )
                    .where(
                        DeliveryAttempt.response_body.is_not(None),
                        DeliveryAttempt.response_purged_at.is_(None),
                        _expired_by_policy(
                            session,
                            DeliveryAttempt.finished_at,
                            OrganizationPolicy.response_retention_days,
                        ),
                    )
                    .order_by(DeliveryAttempt.finished_at, DeliveryAttempt.id)
                    .limit(self.settings.lifecycle_cleanup_batch_size)
                )
                attempts = list(
                    await session.scalars(
                        _claim_rows(statement, session, DeliveryAttempt)
                    )
                )
                for attempt in attempts:
                    attempt.response_body = None
                    attempt.response_purged_at = purged_at
                return LifecycleRunResult(responses_purged=len(attempts))

    async def _purge_event_payloads(self) -> LifecycleRunResult:
        async with self.session_factory() as session:
            async with session.begin():
                purged_at = await _database_now(session)
                has_nonterminal_delivery = exists(
                    select(Delivery.id).where(
                        Delivery.event_id == Event.id,
                        Delivery.status.not_in(_TERMINAL_DELIVERY_STATUSES),
                    )
                )
                statement = (
                    select(Event)
                    .join(Project, Project.id == Event.project_id)
                    .join(
                        OrganizationPolicy,
                        OrganizationPolicy.organization_id
                        == Project.organization_id,
                    )
                    .where(
                        Event.payload_purged_at.is_(None),
                        or_(
                            Event.payload.is_not(None),
                            Event.canonical_envelope.is_not(None),
                        ),
                        ~has_nonterminal_delivery,
                        _expired_by_policy(
                            session,
                            Event.created_at,
                            OrganizationPolicy.payload_retention_days,
                        ),
                    )
                    .order_by(Event.created_at, Event.id)
                    .limit(self.settings.lifecycle_cleanup_batch_size)
                )
                events = list(
                    await session.scalars(
                        _claim_rows(statement, session, Event)
                    )
                )
                if not events:
                    return LifecycleRunResult()

                event_ids = [event.id for event in events]
                unsafe_ids = set(
                    await session.scalars(
                        select(Delivery.event_id)
                        .where(
                            Delivery.event_id.in_(event_ids),
                            Delivery.status.not_in(
                                _TERMINAL_DELIVERY_STATUSES
                            ),
                        )
                        .distinct()
                    )
                )
                purged = 0
                for event in events:
                    if event.id in unsafe_ids:
                        continue
                    event.payload = None
                    event.canonical_envelope = None
                    event.payload_purged_at = purged_at
                    purged += 1
                return LifecycleRunResult(payloads_purged=purged)

    async def _cleanup_due_organization(self) -> LifecycleRunResult:
        async with self.session_factory() as session:
            async with session.begin():
                now = await _database_now(session)
                operation = await self._claim_operation(session, now)
                if operation is None:
                    return LifecycleRunResult()
                organization = await session.scalar(
                    _claim_rows(
                        select(Organization).where(
                            Organization.id == operation.organization_id
                        ),
                        session,
                        Organization,
                    )
                )
                if organization is None:
                    return LifecycleRunResult()
                if organization.lifecycle_state != "deletion_pending":
                    operation.status = "canceled"
                    operation.completed_at = now
                    return LifecycleRunResult()

                operation.status = "running"
                operation.error = None
                canceled = await self._cancel_inactive_deliveries(
                    session, organization.id, now
                )
                if canceled:
                    return LifecycleRunResult(deliveries_canceled=canceled)
                if await self._has_live_delivery(
                    session, organization.id, now
                ):
                    return LifecycleRunResult()

                deleted = await self._delete_delivery_batch(
                    session, organization.id
                )
                if deleted:
                    return LifecycleRunResult(deliveries_deleted=deleted)
                events_deleted = await self._delete_orphan_event_batch(
                    session, organization.id
                )
                if events_deleted:
                    return LifecycleRunResult(events_deleted=events_deleted)
                return await self._delete_empty_organization(
                    session, operation, organization, now
                )

    async def _claim_operation(
        self, session: AsyncSession, now: datetime
    ) -> OrganizationLifecycleOperation | None:
        statement = (
            select(OrganizationLifecycleOperation)
            .join(
                Organization,
                Organization.id
                == OrganizationLifecycleOperation.organization_id,
            )
            .where(
                OrganizationLifecycleOperation.kind == "deletion",
                OrganizationLifecycleOperation.status.in_(
                    ("pending", "running")
                ),
                OrganizationLifecycleOperation.scheduled_at <= now,
                Organization.lifecycle_state == "deletion_pending",
            )
            .order_by(
                OrganizationLifecycleOperation.scheduled_at,
                OrganizationLifecycleOperation.id,
            )
            .limit(1)
        )
        return await session.scalar(
            _claim_rows(statement, session, OrganizationLifecycleOperation)
        )

    async def _cancel_inactive_deliveries(
        self,
        session: AsyncSession,
        organization_id: int,
        now: datetime,
    ) -> int:
        cancellable = or_(
            Delivery.status.in_(_QUEUED_DELIVERY_STATUSES),
            and_(
                Delivery.status == "processing",
                or_(
                    Delivery.lease_expires_at.is_(None),
                    Delivery.lease_expires_at <= now,
                ),
            ),
        )
        statement = (
            select(Delivery)
            .where(
                Delivery.organization_id == organization_id,
                cancellable,
            )
            .order_by(Delivery.id)
            .limit(self.settings.lifecycle_cleanup_batch_size)
        )
        deliveries = list(
            await session.scalars(_claim_rows(statement, session, Delivery))
        )
        for delivery in deliveries:
            delivery.status = "canceled"
            delivery.canceled_at = now
            delivery.canceled_reason = "organization_deletion"
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = now
        return len(deliveries)

    async def _has_live_delivery(
        self,
        session: AsyncSession,
        organization_id: int,
        now: datetime,
    ) -> bool:
        delivery_id = await session.scalar(
            select(Delivery.id)
            .where(
                Delivery.organization_id == organization_id,
                Delivery.status == "processing",
                Delivery.lease_expires_at > now,
            )
            .limit(1)
        )
        return delivery_id is not None

    async def _delete_delivery_batch(
        self, session: AsyncSession, organization_id: int
    ) -> int:
        statement = (
            select(Delivery.id)
            .where(Delivery.organization_id == organization_id)
            .order_by(Delivery.id)
            .limit(self.settings.lifecycle_cleanup_batch_size)
        )
        delivery_ids = list(
            await session.scalars(_claim_rows(statement, session, Delivery))
        )
        if delivery_ids:
            await session.execute(
                delete(Delivery).where(Delivery.id.in_(delivery_ids))
            )
        return len(delivery_ids)

    async def _delete_orphan_event_batch(
        self, session: AsyncSession, organization_id: int
    ) -> int:
        has_delivery = exists(
            select(Delivery.id).where(Delivery.event_id == Event.id)
        )
        statement = (
            select(Event.id)
            .join(Project, Project.id == Event.project_id)
            .where(
                Project.organization_id == organization_id,
                ~has_delivery,
            )
            .order_by(Event.id)
            .limit(self.settings.lifecycle_cleanup_batch_size)
        )
        event_ids = list(
            await session.scalars(_claim_rows(statement, session, Event))
        )
        if event_ids:
            await session.execute(delete(Event).where(Event.id.in_(event_ids)))
        return len(event_ids)

    async def _delete_empty_organization(
        self,
        session: AsyncSession,
        operation: OrganizationLifecycleOperation,
        organization: Organization,
        now: datetime,
    ) -> LifecycleRunResult:
        delivery_count = await session.scalar(
            select(func.count(Delivery.id)).where(
                Delivery.organization_id == organization.id
            )
        )
        event_count = await session.scalar(
            select(func.count(Event.id))
            .join(Project, Project.id == Event.project_id)
            .where(Project.organization_id == organization.id)
        )
        if int(delivery_count or 0) or int(event_count or 0):
            return LifecycleRunResult()

        operation.status = "completed"
        operation.completed_at = now
        await append_audit(
            session,
            operation.requested_by_user_id,
            organization.public_id,
            "organization.deleted",
            "organization",
            organization.public_id,
            metadata={"operation_public_id": operation.public_id},
        )
        await session.flush()
        await session.delete(organization)
        return LifecycleRunResult(organizations_deleted=1)


async def run_lifecycle_loop(
    service: LifecycleService,
    stop_event: asyncio.Event,
) -> None:
    """Run maintenance periodically and wake promptly for worker shutdown."""
    while not stop_event.is_set():
        try:
            await service.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Lifecycle maintenance failed (%s)", type(exc).__name__
            )
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=service.settings.lifecycle_cleanup_interval_seconds,
            )
        except TimeoutError:
            pass
