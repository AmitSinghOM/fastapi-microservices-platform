"""Optional SQLAlchemy transactional outbox with at-least-once relay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from random import SystemRandom
from typing import Any, Literal, Protocol
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from webhook_platform_sdk.producer import ApiError, WebhookPlatformError

EnvelopeMode = Literal["native", "cloudevents"]
metadata = MetaData()
outbox_events = Table(
    "webhook_outbox",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("event_type", String(150), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("envelope_mode", String(16), nullable=False, default="native"),
    Column("lease_token", String(36)),
    Column("locked_until", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("next_attempt_at", DateTime(timezone=True)),
    Column("last_error_code", String(64)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True)),
    Column("failed_at", DateTime(timezone=True)),
)


class EventProducer(Protocol):
    def send_event(
        self,
        event_type: str,
        payload: object,
        idempotency_key: str,
        *,
        envelope_mode: EnvelopeMode = "native",
    ) -> dict[str, Any]: ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_outbox_schema(engine: Engine) -> None:
    """Create the SDK-owned table; production users should use a migration."""
    metadata.create_all(engine, tables=[outbox_events])


def enqueue_outbox(
    session: Session,
    event_type: str,
    payload: object,
    *,
    idempotency_key: str | None = None,
    envelope_mode: EnvelopeMode = "native",
) -> str:
    """Insert into the caller's current transaction without committing it."""
    if not event_type:
        raise ValueError("event_type is required")
    if envelope_mode not in {"native", "cloudevents"}:
        raise ValueError("envelope_mode is invalid")
    key = idempotency_key or str(uuid4())
    created_at = utcnow()
    session.execute(
        outbox_events.insert().values(
            id=str(uuid4()),
            event_type=event_type,
            payload=payload,
            idempotency_key=key,
            envelope_mode=envelope_mode,
            attempt_count=0,
            next_attempt_at=created_at,
            created_at=created_at,
        )
    )
    return key


@dataclass(frozen=True)
class OutboxMessage:
    id: str
    event_type: str
    payload: object
    idempotency_key: str
    envelope_mode: EnvelopeMode
    lease_token: str
    attempt_number: int


class OutboxRelay:
    """Claim briefly, send outside the transaction, then guard finalization."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        producer: EventProducer,
        *,
        batch_size: int = 50,
        lease_seconds: int = 60,
        max_attempts: int = 20,
        retry_base_seconds: float = 1.0,
        retry_cap_seconds: float = 300.0,
    ) -> None:
        if not 1 <= batch_size <= 1_000:
            raise ValueError("batch_size must be between 1 and 1000")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not 0 <= retry_base_seconds <= retry_cap_seconds:
            raise ValueError("retry delay bounds are invalid")
        self._session_factory = session_factory
        self._producer = producer
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._random = SystemRandom()

    def _claim(self) -> list[OutboxMessage]:
        now = utcnow()
        claimed: list[OutboxMessage] = []
        with self._session_factory() as session, session.begin():
            rows = session.execute(
                select(outbox_events)
                .where(
                    outbox_events.c.sent_at.is_(None),
                    outbox_events.c.failed_at.is_(None),
                    outbox_events.c.next_attempt_at <= now,
                    or_(
                        outbox_events.c.locked_until.is_(None),
                        outbox_events.c.locked_until <= now,
                    ),
                )
                .order_by(outbox_events.c.created_at, outbox_events.c.id)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            ).mappings()
            for row in rows:
                token = str(uuid4())
                session.execute(
                    update(outbox_events)
                    .where(outbox_events.c.id == row["id"])
                    .values(
                        lease_token=token,
                        locked_until=now
                        + timedelta(seconds=self._lease_seconds),
                        attempt_count=outbox_events.c.attempt_count + 1,
                    )
                )
                claimed.append(
                    OutboxMessage(
                        id=row["id"],
                        event_type=row["event_type"],
                        payload=row["payload"],
                        idempotency_key=row["idempotency_key"],
                        envelope_mode=row["envelope_mode"],
                        lease_token=token,
                        attempt_number=row["attempt_count"] + 1,
                    )
                )
        return claimed

    def _finish(
        self,
        message: OutboxMessage,
        *,
        sent: bool,
        error_code: str | None = None,
        retry_at: datetime | None = None,
        terminal: bool = False,
    ) -> None:
        values: dict[str, object | None] = {
            "lease_token": None,
            "locked_until": None,
            "last_error_code": error_code,
            "next_attempt_at": retry_at,
        }
        if sent:
            values["sent_at"] = utcnow()
        elif terminal:
            values["failed_at"] = utcnow()
        with self._session_factory() as session, session.begin():
            session.execute(
                update(outbox_events)
                .where(
                    outbox_events.c.id == message.id,
                    outbox_events.c.lease_token == message.lease_token,
                    outbox_events.c.sent_at.is_(None),
                )
                .values(**values)
            )

    def _record_failure(
        self,
        message: OutboxMessage,
        code: str,
        *,
        retryable: bool,
        retry_after: int | None = None,
    ) -> None:
        terminal = (
            not retryable or message.attempt_number >= self._max_attempts
        )
        retry_at: datetime | None = None
        if not terminal:
            cap = min(
                self._retry_cap_seconds,
                self._retry_base_seconds * 2 ** (message.attempt_number - 1),
            )
            delay = self._random.uniform(0, cap)
            if retry_after is not None:
                delay = max(delay, min(retry_after, self._retry_cap_seconds))
            retry_at = utcnow() + timedelta(seconds=delay)
        self._finish(
            message,
            sent=False,
            error_code=code[:64],
            retry_at=retry_at,
            terminal=terminal,
        )

    def run_once(self) -> tuple[int, int]:
        """Return successful and failed counts for one bounded batch.

        A crash after API acceptance but before `_finish` causes another relay
        attempt with the same idempotency key. Transient retries use bounded
        full jitter and stop at `max_attempts`; terminal API responses stop
        immediately.
        """
        succeeded = 0
        failed = 0
        for message in self._claim():
            try:
                self._producer.send_event(
                    message.event_type,
                    message.payload,
                    message.idempotency_key,
                    envelope_mode=message.envelope_mode,
                )
            except ApiError as exc:
                retryable = exc.status_code in {408, 425, 429, 503}
                retryable = retryable or exc.status_code >= 500
                self._record_failure(
                    message,
                    exc.code,
                    retryable=retryable,
                    retry_after=exc.retry_after,
                )
                failed += 1
            except WebhookPlatformError as exc:
                self._record_failure(
                    message, type(exc).__name__, retryable=True
                )
                failed += 1
            else:
                self._finish(message, sent=True)
                succeeded += 1
        return succeeded, failed
